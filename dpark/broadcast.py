import os, time
import uuid
import socket
import marshal
import cPickle
import threading
import logging
from multiprocessing import Lock
try:
    from setproctitle import getproctitle, setproctitle
except ImportError:
    def getproctitle():
        return ''
    def setproctitle(x):
        pass

import zmq

import cache
from env import env

logger = logging.getLogger("broadcast")

class SourceInfo:
    TxNotStartedRetry = -1
    TxOverGoToHDFS = 0
    StopBroadcast = -2
    UnusedParam = 0
    def __init__(self, addr, total_blocks=0, total_bytes=0, block_size=0):
        self.addr = addr
        self.total_blocks = total_blocks
        self.total_bytes = total_bytes
        self.block_size = block_size

        self.leechers = 0
        self.failed = False

    def __cmp__(self, other):
        return self.leechers - other.leechers

    def __str__(self):
        return "<source %s>" % (self.addr)

class BroadcastBlock:
    def __init__(self, id, data):
        self.id = id
        self.data = data

class VariableInfo:
    def __init__(self, blocks, total_blocks, total_bytes):
        self.blocks = blocks
        self.total_blocks = total_blocks
        self.total_bytes = total_bytes
        self.has_blocks = 0

class Broadcast:
    initialized = False
    is_master = False
    cache = cache.Cache() 
    broadcastFactory = None
    BlockSize = 4096 * 1024
    MaxRetryCount = 2
    MinKnockInterval = 500
    MaxKnockInterval = 999
        
    def __init__(self, value, is_local):
        self.uuid = str(uuid.uuid4())
        self.value = value
        if is_local:
            if not self.cache.put(self.uuid, value):
                raise Exception('object %s is too big to cache', repr(value))
        else:
            self.sendBroadcast()

    def __getstate__(self):
        return self.uuid

    def __setstate__(self, uuid):
        self.uuid = uuid
    
    def __getattr__(self, name):
        if name != 'value':
            raise AttributeError(name)

        # in the executor process, Broadcast is not initialized
        if not self.initialized:
            return

        uuid = self.uuid
        self.value = self.cache.get(uuid)
        if self.value is not None:
            return self.value    
        
        oldtitle = getproctitle()
        setproctitle('dpark worker: broadcasting ' + uuid)

        self.recvBroadcast()
        if self.value is None:
            raise Exception("recv broadcast failed")
        self.cache.put(uuid, self.value)

        setproctitle(oldtitle)
        return self.value                
                
    def sendBroadcast(self):
        raise NotImplementedError

    def recvBroadcast(self):
        raise NotImplementedError

    def blockifyObject(self, obj):
        try:
            buf = marshal.dumps(obj)
        except ValueError:
            buf = cPickle.dumps(obj, -1)
        N = self.BlockSize
        blockNum = len(buf) / N
        if len(buf) % N != 0:
            blockNum += 1
        val = [BroadcastBlock(i/N, buf[i:i+N]) 
                    for i in range(0, len(buf), N)]
        vi = VariableInfo(val, blockNum, len(buf))
        vi.has_blocks = blockNum
        return vi

    def unBlockifyObject(self, blocks):
        s = ''.join(b.data for b in blocks)
        try:
            return marshal.loads(s)
        except ValueError:
            return cPickle.loads(s)
   
    @classmethod
    def initialize(cls, is_master):
        if cls.initialized:
            return

        cls.is_master = is_master
        cls.host = socket.gethostname()

#        cls.broadcastFactory = FileBroadcastFactory()
        cls.broadcastFactory = TreeBroadcastFactory()
        cls.broadcastFactory.initialize(is_master)
        cls.initialized = True
        logger.debug("Broadcast initialized")

    @classmethod
    def getBroadcastFactory(cls):
        return cls.broadcastFactory

    @classmethod
    def newBroadcast(cls, value, is_local):
        return cls.broadcastFactory.newBroadcast(value, is_local)

class BroadcastFactory:
    def initialize(self, is_master):
        raise NotImplementedError
    def newBroadcast(self, value, is_local):
        raise NotImplementedError


class FileBroadcast(Broadcast):
    @property
    def path(self):
        return os.path.join(self.workdir, self.uuid)

    def sendBroadcast(self):
        f = open(self.path, 'wb', 65536*100)
        try:
            marshal.dump(self.value, f)
        except ValueError:
            cPickle.dump(self.value, f, -1)
        f.close()
        logger.debug("dump to %s", self.path)

    def recvBroadcast(self):
        try:
            self.value = marshal.load(open(self.path, 'rb', 65536*100))
        except ValueError:
            self.value = cPickle.load(open(self.path, 'rb', 65536*100))
        logger.debug("load from %s", self.path)

    workdir = None
    compress = False
    @classmethod
    def initialize(cls, is_master):
        cls.workdir = env.get('WORKDIR')
        logger.debug("FileBroadcast initialized")

class FileBroadcastFactory:
    def initialize(self, is_master):
        return FileBroadcast.initialize(is_master)
    def newBroadcast(self, value, is_local):
        return FileBroadcast(value, is_local)


class TreeBroadcast(FileBroadcast):
    def __init__(self, value, is_local):
        self.initializeSlaveVariables()
        Broadcast.__init__(self, value, is_local)

    def initializeSlaveVariables(self):    
        self.blocks = []
        self.total_bytes = -1
        self.total_blocks = -1
        self.block_size = self.BlockSize

        self.listOfSources = {}
        self.serverAddr = None
        self.guide_addr = None

        self.has_copy_in_fs = False
        self.stop = False

    def sendBroadcast(self):
        # store a copy to file
        # FileBroadcast.sendBroadcast(self)
        # self.has_copy_in_fs = True
        logger.debug("start sendBroadcast %s", self.uuid)
        variableInfo = self.blockifyObject(self.value)
        self.blocks = variableInfo.blocks
        self.total_bytes = variableInfo.total_bytes
        self.total_blocks = variableInfo.total_blocks

        self.startGuide()
        self.startServer()
        
    def startGuide(self):
        def run():
            setOfCompletedSources = set()
            ctx = zmq.Context()
            sock = ctx.socket(zmq.REP)
            port = sock.bind_to_random_port("tcp://0.0.0.0")
            self.guide_addr = "tcp://%s:%d" % (self.host, port)
            logger.debug("guide start at %s", self.guide_addr)

            while True:
                if self.stop and self.has_copy_in_fs:
                    break
                #Stop broadcast if at least one worker has connected and
                #everyone connected so far are done. Comparing with
                #listOfSources.size - 1, because it includes the Guide itself
                if (len(self.listOfSources) > 1 
                    and len(setOfCompletedSources) == len(self.listOfSources) -1):
                    self.stop = True
                    break
                o = sock.recv_pyobj()
                if isinstance(o, SourceInfo):
                    ssi = self.selectSuitableSource(o)
                    logger.debug("sending selected sourceinfo %s", ssi.addr)
                    sock.send_pyobj(ssi)
                    o = SourceInfo(o.addr, self.total_blocks,
                        self.total_bytes, self.block_size)
                    logger.debug("Adding possible new source to listOfSource: %s",
                        o)
                    self.listOfSources[o.addr] = o

            sock.close()
            logger.debug("Sending stop notification ...")

            for source_info in self.listOfSources.values():
                req = ctx.socket(zmq.REQ)
                req.send_pyobj(SourceInfo.StopBroadcast)
                #req.recv_pyobj()
                req.close()
            self.unregisterValue(self.uuid)

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        # wait for guide to start
        while self.guide_addr is None:
            time.sleep(0.01)
        self.registerValue(self.uuid, self.guide_addr)
        logger.debug("guide started...")

    def selectSuitableSource(self, skip):
        maxLeechers = -1
        selectedSource = None
        for s in self.listOfSources.values():
            if (s.addr != skip.addr 
                    and s.leechers < self.MaxDegree
                    and s.leechers > maxLeechers):
                selectedSource = s
                maxLeechers = s.leechers
        selectedSource.leechers += 1
        return selectedSource

    def startServer(self):
        def run():
            ctx = zmq.Context()
            sock = ctx.socket(zmq.REP)
            port = sock.bind_to_random_port("tcp://0.0.0.0")
            self.serverAddr = 'tcp://%s:%d' % (self.host,port)
            logger.debug("server started at %s", self.serverAddr)

            while True:
                if self.stop:
                    break
                id = sock.recv_pyobj()
                if id == SourceInfo.StopBroadcast:
                    self.stop = True
                    # TODO send to gruide server
                    break
                while id >= len(self.blocks):
                    time.sleep(0.01)
                if not isinstance(self.blocks[id], BroadcastBlock):
                    raise Exception("bad block: %s" % repr(self.blocks[id]))
                sock.send_pyobj(self.blocks[id])
            sock.close()
            logger.debug("stop TreeBroadcast server %s", self.serverAddr)

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()
        while self.serverAddr is None:
            time.sleep(0.01)
        #logger.debug("server started...")
        self.listOfSources[self.serverAddr] = SourceInfo(self.serverAddr, 
            self.total_blocks, self.total_bytes,
            self.block_size)

    def recvBroadcast(self):
        self.initializeSlaveVariables()
        
        self.startServer()

        start = time.time()
        suc = self.receiveBroadcast(self.uuid)
        if suc:
            self.value = self.unBlockifyObject(self.blocks)
        else:    
            # fallback
            logger.warning("recieve obj failed, fallback to FileBroadcast")
            FileBroadcast.recvBroadcast(self)
        used = time.time() - start
        logger.debug("Reading Broadcasted variable %s took %ss", self.uuid, used)

    def receiveBroadcast(self, uuid):
        master_addr = self.getMasterAddr(uuid)
        if (master_addr == SourceInfo.TxOverGoToHDFS
            or master_addr == SourceInfo.TxNotStartedRetry):
            return False
        while self.serverAddr is None:
            time.sleep(0.01)
        
        ctx = zmq.Context()
        guide_sock = ctx.socket(zmq.REQ)
        guide_sock.connect(master_addr)
        logger.debug("connect to guide %s", master_addr)

        guide_sock.send_pyobj(SourceInfo(self.serverAddr))
        source_info = guide_sock.recv_pyobj()
        self.total_blocks = source_info.total_blocks
        self.total_bytes = source_info.total_bytes
        self.blocks = []
        logger.debug("received SourceInfo from master: %s", 
            source_info)

        #start = time.time()
        suc = self.receiveSingleTransmission(source_info)
        if not suc:
            source_info.failed = True

#        guide_sock.send_pyobj(source_info)
#        guide_sock.recv_pyobj()

        return len(self.blocks) == self.total_blocks

    def receiveSingleTransmission(self, source_info):
        receptionSucceeded = False
        logger.debug("Inside receiveSingleTransmission")
        logger.debug("total_blocks: %s has %s", self.total_blocks,
                len(self.blocks))
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.connect(source_info.addr)
        for i in range(source_info.total_blocks):
            sock.send_pyobj(i)
            block = sock.recv_pyobj()
            if i != block.id:
                raise Exception("bad block %d %s", i, block)
            logger.debug("Received block: %s from %s", 
                block.id, source_info.addr)
            self.blocks.append(block)
            receptionSucceeded = True
        return receptionSucceeded

    def getMasterAddr(self, uuid):
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.connect(self.master_addr)
        sock.send_pyobj(uuid)
        guide_addr = sock.recv_pyobj()
        sock.close()
        return guide_addr

    guides = {}
    MaxDegree = 4
    master_addr = None

    @classmethod
    def initialize(cls, is_master):

        FileBroadcast.initialize(is_master)

        def run():
            ctx = zmq.Context()
            sock = ctx.socket(zmq.REP)
            port = sock.bind_to_random_port("tcp://0.0.0.0")
            cls.master_addr = 'tcp://%s:%d' % (cls.host, port)
            logger.debug("TreeBroadcast tracker started at %s", 
                    cls.master_addr)
            while True:
                uuid = sock.recv_pyobj()
                guide = cls.guides.get(uuid, '')
                if not guide:
                    logger.warning("broadcast %s is not registered", uuid)
                sock.send_pyobj(guide)
            sock.close()
            logger.debug("TreeBroadcast tracker stopped")

        if is_master:
            t = threading.Thread(target=run)
            t.daemon = True
            t.start()
            while cls.master_addr is None:
                time.sleep(0.01)
            env.register('TreeBroadcastTrackerAddr', cls.master_addr)
        else:
            cls.master_addr = env.get('TreeBroadcastTrackerAddr')
            
        logger.debug("TreeBroadcast initialized")

    @classmethod
    def registerValue(cls, uuid, guide_addr):
        cls.guides[uuid] = guide_addr
        logger.debug("New value registered with the Tracker %s, %s", uuid, guide_addr) 

    @classmethod
    def unregisterValue(cls, uuid):
        guide_addr = cls.guides.pop(uuid, None)
        logger.debug("value unregistered from Tracker %s, %s", uuid, guide_addr) 

class TreeBroadcastFactory(BroadcastFactory):
    def initialize(self, is_master):
        return TreeBroadcast.initialize(is_master)
    def newBroadcast(self, value, is_local):
        return TreeBroadcast(value, is_local)

def _test_init():
    Broadcast.initialize(False)

def _test_in_process(v):
    assert v.value[0] == 0
    assert len(v.value) == 1000*1000

if __name__ == '__main__':
    import logging
    logging.basicConfig(
        format="%(process)d:%(threadName)s:%(levelname)s %(message)s",
        level=logging.DEBUG)
    Broadcast.initialize(True)
    import multiprocessing
    from env import env
    pool = multiprocessing.Pool(4, _test_init)

    v = range(1000*1000)
    b = Broadcast.newBroadcast(v, False)
    b = cPickle.loads(cPickle.dumps(b, -1))
    assert len(b.value) == len(v), b.value

    for i in range(10):
        pool.apply_async(_test_in_process, [b])
    time.sleep(3)

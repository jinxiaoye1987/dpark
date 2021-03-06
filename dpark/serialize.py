import marshal, new, cPickle

def dump_object(o):
    if type(o) == type(marshal):
        return 2, o.__name__
    if isinstance(o, new.function) and o.__module__ == '__main__':
        return 1, dump_func(o)
    try:
        return 3, cPickle.dumps(o, -1)
    except Exception:
        if isinstance(o, new.function): # lambda in module
            return 1, dump_func(o)
        print 'unable to pickle:', o
        raise

def load_object((t, d)):
    if t == 1:
        return load_func(d)
    elif t == 2:
        return __import__(d)
    elif t == 3:
        return cPickle.loads(d)
    else:
        raise Exception("invalid flag %d" % t)

def dump_func(f):
    if not isinstance(f, new.function):
        return 1, cPickle.dumps(f, -1)
    if f.__module__ != '__main__':
        try:
            return 1, cPickle.dumps(f, -1)
        except Exception:
            pass

    code = f.func_code
    glob = {}
    for n in code.co_names:
        r = f.func_globals.get(n)
        if r is not None:
            glob[n] = dump_object(r)
    closure = f.func_closure and tuple(dump_object(c.cell_contents) for c in f.func_closure) or None 
    return 0, marshal.dumps((code, glob, f.func_name, f.func_defaults, closure))

def load_func((flag, bytes)):
    if flag == 1:
        return cPickle.loads(bytes)
    code, glob, name, defaults, closure = marshal.loads(bytes)
    glob = dict((k, load_object(v)) for k,v in glob.items())
    glob['__builtins__'] = __builtins__
    closure = closure and reconstruct_closure([load_object(c) for c in closure]) or None
    return new.function(code, glob, name, defaults, closure)

def reconstruct_closure(values):
    ns = range(len(values))
    src = ["def f(arg):"]
    src += [" _%d = arg[%d]" % (n, n) for n in ns]
    src += [" return lambda:(%s)" % ','.join("_%d"%n for n in ns), '']
    src = '\n'.join(src)
    try:
        exec src
    except Exception:
        raise SyntaxError(src)
    values.reverse()
    return f(values).func_closure

if __name__ == "__main__":
    some_global = 'some global'
    def glob_func(s):
        return "glob:" + s
    def get_closure(x):
        glob_func(some_global)
        last = " last"
        def foo(y): return "foo: " + y
        def the_closure(a, b=1):
            marshal.dumps(a)
            return (a * x + int(b), glob_func(foo(some_global)+last))
        return the_closure

#    glob_func = load_func(dump_func(glob_func))
#    get_closure = load_func(dump_func(get_closure))
    f = get_closure(10)
    ff = load_func(dump_func(f))
    #print globals()
    print f(2)
    print ff(2)

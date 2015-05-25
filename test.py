# from topology import PPool
from multiprocessing import Queue, Process
from test2 import Foo, Prt

prt = Prt(123123)
foo1 = Foo(prt)
foo2 = Foo(prt)


class PPool(object):
    def __init__(self, inst, method):
        self.inst = inst
        self.method = method
        self.processes = []

    def submit(self, *args, **kwargs):
        p = Process(target=self.func, args=args, kwargs=kwargs)
        self.processes.append(p)
        p.start()
        if self.single:
            p.join()

    def __enter__(self):
        return self

    def __exit__(self, x, y, z):
        for p in self.processes:
            p.join()



with PPool()
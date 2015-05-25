import logging


class Foo():
    def __init__(self, a):
        self.a = a
    def log(self):
        logging.error(self.a.b)

class Prt():
    def __init__(self, b):
        self.b = b


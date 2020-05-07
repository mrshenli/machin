import dill
import functools as fc
import multiprocessing as mp
import multiprocessing.util as util
import torch.multiprocessing.pool as tmpp
from multiprocessing.dummy import Process as TProcess

from .queue import SimpleQueue


def proxy_short(x):
    func_str, *args = x
    func = dill.loads(func_str)
    return func(*args)


def proxy_long(func_str, *args, **kwargs):
    func = dill.loads(func_str)
    return func(*args, **kwargs)


def proxy_zip_short(recurse, func, iterable):
    dump = dill.dumps(func, protocol=dill.HIGHEST_PROTOCOL, recurse=recurse)
    for arg in iterable:
        yield dump, arg


def proxy_zip_long(recurse, func, iterable):
    dump = dill.dumps(func, protocol=dill.HIGHEST_PROTOCOL, recurse=recurse)
    for args in iterable:
        # recurese will enable contect variable saving
        yield [dump] + list(args)


class Pool(tmpp.Pool):
    is_global = False

    def enable_global(self, is_global):
        self.is_global = is_global

    def enable_copy_tensors(self, is_copy_tensors):
        self._inqueue.set_copy(is_copy_tensors)
        self._outqueue.set_copy(is_copy_tensors)

    def _setup_queues(self):
        """
        There seems to be a bug in the Queue implementation of python3, but SimpleQueue works fine
        in get():

        if block and timeout is None:
            with self._rlock:
                res = self._recv_bytes()
            self._sem.release()
        If the queue is empty, ValueError: semaphore or lock released too many times will be raised
        """

        self._inqueue = SimpleQueue(ctx=self._ctx)
        self._outqueue = SimpleQueue(ctx=self._ctx)
        self._quick_put = self._inqueue._writer.send
        self._quick_get = self._outqueue._reader.recv

    def apply(self, func, args=(), kwds={}):
        """
        Equivalent of `func(*args, **kwds)`.
        """

        return tmpp.Pool.apply(self, proxy_long,
                               [dill.dumps(func, recurse=self.is_global)] + list(args), kwds)

    def map(self, func, iterable, chunksize=None):
        """
        Apply `func` to each element in `iterable`, collecting the results
        in a list that is returned.
        """
        return tmpp.Pool.map(self, proxy_short,
                             fc.partial(proxy_zip_short, self.is_global)(func, iterable),
                             chunksize)

    def starmap(self, func, iterable, chunksize=None):
        """
        Like `map()` method but the elements of the `iterable` are expected to
        be iterables as well and will be unpacked as arguments. Hence
        `func` and (a, b) becomes func(a, b).
        """
        return tmpp.Pool.starmap(self, proxy_long,
                                 fc.partial(proxy_zip_long, self.is_global)(func, iterable),
                                 chunksize)

    def starmap_async(self, func, iterable, chunksize=None, callback=None,
                      error_callback=None):
        """
        Asynchronous version of `starmap()` method.
        """
        return tmpp.Pool.starmap_async(self, proxy_long,
                                       fc.partial(proxy_zip_long, self.is_global)(func, iterable),
                                       chunksize, callback, error_callback)

    def imap(self, func, iterable, chunksize=1):
        """
        Equivalent of `map()` -- can be MUCH slower than `Pool.map()`.
        """
        return tmpp.Pool.imap(self, proxy_short,
                              fc.partial(proxy_zip_short, self.is_global)(func, iterable),
                              chunksize)

    def imap_unordered(self, func, iterable, chunksize=1):
        """
        Like `imap()` method but ordering of results is arbitrary.
        """
        return tmpp.Pool.imap_unordered(self, proxy_short,
                                        fc.partial(proxy_zip_short, self.is_global)(func, iterable),
                                        chunksize)

    def apply_async(self, func, args=(), kwds={}, callback=None,
                    error_callback=None):
        """
        Asynchronous version of `apply()` method.
        """
        return tmpp.Pool.apply_async(self, proxy_short,
                                     [dill.dumps(func, recurse=self.is_global)] + list(args), kwds)

    def map_async(self, func, iterable, chunksize=None, callback=None,
                  error_callback=None):
        """
        Asynchronous version of `map()` method.
        """
        return tmpp.Pool.map(self, proxy_short,
                             fc.partial(proxy_zip_short, self.is_global)(func, iterable),
                             chunksize, callback, error_callback)


class ThreadPool(mp.pool.ThreadPool):
    def _repopulate_pool(self):
        """Bring the number of pool processes up to the specified number,
        for use after reaping workers which have exited.
        """
        for i in range(self._processes - len(self._pool)):
            # changed worker -> clean_worker
            args = (self._inqueue, self._outqueue,
                    self._initializer,
                    self._initargs, self._maxtasksperchild)
            if hasattr(self, '_wrap_exception'):
                args += (self._wrap_exception,)
            w = self.Process(target=tmpp.clean_worker, args=args)
            self._pool.append(w)
            w.name = w.name.replace('Process', 'PoolWorker')
            w.daemon = True
            w.start()
            util.debug('added worker')
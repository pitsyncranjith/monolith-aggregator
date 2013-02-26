import gevent
from gevent.queue import JoinableQueue
from gevent.pool import Group

from aggregator import logger


class AlreadyDoneError(Exception):
    pass


class Engine(object):

    def __init__(self, sequence, history, phase_hook=None, batch_size=100,
                 force=False, retries=3):
        self.sequence = sequence
        self.history = history
        self.queue = JoinableQueue()
        self.phase_hook = phase_hook
        self.batch_size = batch_size
        self.force = force
        self.retries = retries

    def _push_to_target(self, targets):
        """Get a batch of elements from the queue, and push it to the targets.

        This function returns True if it proceeded all the elements in
        the queue, and there isn't anything more to read.
        """
        if self.queue.empty():
            return False    # nothing

        batch = []
        eoq = False

        # collecting a batch
        while len(batch) < self.batch_size:
            try:
                item = self.queue.get()

                if item == 'END':
                    # reached the end
                    eoq = True
                    break
                batch.append(item)
            finally:
                self.queue.task_done()

        if len(batch) != 0:
            greenlets = Group()
            for plugin in targets:
                greenlets.spawn(self._put_data, plugin, batch)

            greenlets.join()
        return eoq

    #
    # transaction managment
    #
    def _start_transactions(self, plugins):
        for plugin in plugins:
            plugin.start_transaction()

    def _commit_transactions(self, plugins):
        # XXX what happends when this fails?
        for plugin in plugins:
            plugin.commit_transaction()

    def _rollback_transactions(self, plugins):
        for plugin in plugins:
            plugin.rollback_transaction()

    def _put_data(self, plugin, data):
        return plugin.inject(data, overwrite=self.force)

    def _get_data(self, plugin, start_date, end_date):
        try:
            for item in plugin.extract(start_date, end_date):
                self.queue.put((plugin.get_id(), item))
        finally:
            self.queue.put('END')

    def _extract_inject(self, start_date, end_date):
        for phase, sources, targets in self.sequence:
            for source in sources:
                exists = self.history.exists(source, start_date, end_date)
                if exists and not self.force:
                    raise AlreadyDoneError()

            logger.info('Running phase %r' % phase)

            self._start_transactions(targets)
            self.history.start_transaction()
            try:
                greenlets = Group()
                # each callable will push its result in the queue
                for source in sources:
                    greenlets.spawn(self._get_data, source, start_date,
                                    end_date)
                # looking at the queue
                processed = 0
                while processed < len(sources):
                    eoq = self._push_to_target(targets)
                    if eoq:
                        processed += 1
                    gevent.sleep(0)

                greenlets.join()
                self.history.add_entry(sources, start_date, end_date)
            except Exception:
                self._rollback_transactions(targets)
                self.history.rollback_transaction()
                raise
            else:
                self._commit_transactions(targets)
                self.history.commit_transaction()

    def _purge(self, start_date, end_date):
        for phase, sources, targets in self.sequence:
            for source in sources:
                try:
                    source.purge(start_date, end_date)
                except Exception:
                    logger.error('Failed to purge %r' % source.get_id())

    def _retry(self, func, *args, **kw):
        tries = 0
        retries = self.retries
        while tries < retries:
            try:
                return func(*args, **kw)
            except Exception, exc:
                if isinstance(exc, AlreadyDoneError):
                    raise
                logger.exception('%s failed (%d/%d)' % (func, tries + 1,
                                                        retries))
                tries += 1
        raise

    def run(self, start_date, end_date, purge_only=False):
        if not purge_only:
            self._retry(self._extract_inject, start_date, end_date)

        self._retry(self._purge, start_date, end_date)
        return 0

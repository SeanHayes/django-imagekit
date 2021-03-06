from ..utils import get_singleton, sanitize_cache_key
from copy import copy
from django.core.cache import get_cache
from django.core.exceptions import ImproperlyConfigured


class CacheFileState(object):
    EXISTS = 'exists'
    GENERATING = 'generating'
    DOES_NOT_EXIST = 'does_not_exist'


def get_default_cachefile_backend():
    """
    Get the default file backend.

    """
    from django.conf import settings
    return get_singleton(settings.IMAGEKIT_DEFAULT_CACHEFILE_BACKEND,
            'file backend')


class InvalidFileBackendError(ImproperlyConfigured):
    pass


class AbstractCacheFileBackend(object):
    """
    An abstract cache file backend. This isn't used by any internal classes and
    is included simply to illustrate the minimum interface of a cache file
    backend for users who wish to implement their own.

    """
    def generate(self, file, force=False):
        raise NotImplementedError

    def exists(self, file):
        raise NotImplementedError


class CachedFileBackend(object):
    existence_check_timeout = 5
    """
    The number of seconds to wait before rechecking to see if the file exists.
    If the image is found to exist, that information will be cached using the
    timeout specified in your CACHES setting (which should be very high).
    However, when the file does not exist, you probably want to check again
    in a relatively short amount of time. This attribute allows you to do that.

    """

    @property
    def cache(self):
        if not getattr(self, '_cache', None):
            from django.conf import settings
            self._cache = get_cache(settings.IMAGEKIT_CACHE_BACKEND)
        return self._cache

    def get_key(self, file):
        from django.conf import settings
        return sanitize_cache_key('%s%s-state' %
                                  (settings.IMAGEKIT_CACHE_PREFIX, file.name))

    def get_state(self, file, check_if_unknown=True):
        key = self.get_key(file)
        state = self.cache.get(key)
        if state is None and check_if_unknown:
            exists = self._exists(file)
            state = CacheFileState.EXISTS if exists else CacheFileState.DOES_NOT_EXIST
            self.set_state(file, state)
        return state

    def set_state(self, file, state):
        key = self.get_key(file)
        if state == CacheFileState.DOES_NOT_EXIST:
            self.cache.set(key, state, self.existence_check_timeout)
        else:
            self.cache.set(key, state)

    def __getstate__(self):
        state = copy(self.__dict__)
        # Don't include the cache when pickling. It'll be reconstituted based
        # on the settings.
        state.pop('_cache', None)
        return state

    def exists(self, file):
        return self.get_state(file) == CacheFileState.EXISTS

    def generate(self, file, force=False):
        raise NotImplementedError

    def generate_now(self, file, force=False):
        if force or self.get_state(file) not in (CacheFileState.GENERATING, CacheFileState.EXISTS):
            self.set_state(file, CacheFileState.GENERATING)
            file._generate()
            self.set_state(file, CacheFileState.EXISTS)


class Simple(CachedFileBackend):
    """
    The most basic file backend. The storage is consulted to see if the file
    exists. Files are generated synchronously.

    """

    def generate(self, file, force=False):
        self.generate_now(file, force=force)

    def _exists(self, file):
        return bool(getattr(file, '_file', None)
                    or file.storage.exists(file.name))


def _generate_file(backend, file, force=False):
    backend.generate_now(file, force=force)


try:
    import celery
except ImportError:
    pass
else:
    _generate_file = celery.task(ignore_result=True)(_generate_file)


class Async(Simple):
    """
    A backend that uses Celery to generate the images.
    """

    def __init__(self, *args, **kwargs):
        try:
            import celery
        except ImportError:
            raise ImproperlyConfigured('You must install celery to use'
                                       ' imagekit.cachefiles.backend.Async.')
        super(Async, self).__init__(*args, **kwargs)

    def generate(self, file, force=False):
        # Schedule the file for generation, unless we know for sure we don't
        # need to. If an already-generated file sneaks through, that's okay;
        # ``generate_now`` will catch it. We just want to make sure we don't
        # schedule anything we know is unnecessary--but we also don't want to
        # force a costly existence check.
        if self.get_state(file, check_if_unknown=False) not in (CacheFileState.GENERATING, CacheFileState.EXISTS):
            _generate_file.delay(self, file, force=force)

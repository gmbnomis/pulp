import aiohttp
import atexit
import copy
from gettext import gettext as _
import ssl
from urllib.parse import urlparse

from .http import HttpDownloader
from .file import FileDownloader


PROTOCOL_MAP = {
    'http': HttpDownloader,
    'https': HttpDownloader,
    'file': FileDownloader
}


class DownloaderFactory:
    """
    A factory for creating downloader objects that are configured from with importer settings.

    It supports `http`, `https`, and `file` protocols. The ``downloader_overrides`` option allows
    the caller to specify the download class to be used for any given protocol. This allows the user
    to specify custom, subclassed downloaders to be built by the factory.

    Usage:
        >>> the_factory = DownloaderFactory(importer)
        >>> downloader = the_factory.build(url_a)
        >>> result = downloader.fetch()  # 'result' is a DownloadResult
    """

    def __init__(self, importer, downloader_overrides=None):
        """
        Args:
            importer (:class:`~pulpcore.plugin.models.Importer`): The importer used to populate
                downloader settings.
            downloader_overrides (dict): Keyed on a scheme name, e.g. 'https' or 'ftp' and the value
                is the downloader class to be used for that scheme, e.g.
                {'https': MyCustomDownloader}. These override the default values.
        """
        self._importer = importer
        self._download_class_map = copy.copy(PROTOCOL_MAP)
        if downloader_overrides:
            for protocol, download_class in downloader_overrides.items():  # overlay the overrides
                self._download_class_map[protocol] = download_class
        self._handler_map = {'https': self._http_or_https, 'http': self._http_or_https,
                             'file': self._generic}
        self._session = self._make_aiohttp_session_from_importer()
        atexit.register(self._session.close)

    def _make_aiohttp_session_from_importer(self):
        """
        Build a :class:`aiohttp.ClientSession` from the importer settings

        Returns:
            :class:`aiohttp.ClientSession`
        """
        tcp_conn_opts = {}

        sslcontext = None
        if self._importer.ssl_ca_certificate.name:
            sslcontext = ssl.create_default_context(cafile=self._importer.ssl_ca_certificate.name)
            if self._importer.ssl_client_key.name and self._importer.ssl_client_certificate.name:
                sslcontext.load_cert_chain(
                    self._importer.ssl_client_key.name,
                    self._importer.ssl_client_certificate.name
                )
        else:
            if self._importer.ssl_client_key.name and self._importer.ssl_client_certificate.name:
                sslcontext = ssl.create_default_context()
                sslcontext.load_cert_chain(
                    self._importer.ssl_client_key.name,
                    self._importer.ssl_client_certificate.name
                )

        if sslcontext:
            tcp_conn_opts['ssl_context'] = sslcontext

        if self._importer.ssl_validation:
            tcp_conn_opts['verify_ssl'] = self._importer.ssl_validation

        conn = aiohttp.TCPConnector(**tcp_conn_opts)

        auth_options = {}
        if self._importer.username and self._importer.password:
            auth_options['auth'] = aiohttp.BasicAuth(
                login=self._importer.username,
                password=self._importer.password
            )

        return aiohttp.ClientSession(connector=conn, **auth_options)

    def build(self, url, **kwargs):
        """
        Build a downloader which can optionally verify integrity using either digest or size.

        Args:
            url (str): The download URL.
            kwargs (dict): All kwargs are passed along to the downloader. At a minimum, these
                include the :class:`~pulpcore.plugin.download.BaseDownloader` parameters.

        Returns:
            subclass of :class:`~pulpcore.plugin.download.BaseDownloader`: A downloader that
            is configured with the importer settings.
        """
        scheme = urlparse(url).scheme.lower()
        try:
            builder = self._handler_map[scheme]
            download_class = self._download_class_map[scheme]
        except KeyError:
            raise ValueError(_('URL: {u} not supported.'.format(u=url)))
        else:
            return builder(download_class, url, **kwargs)

    def _http_or_https(self, download_class, url, **kwargs):
        """
        Build a downloader for http:// or https:// URLs.

        Args:
            download_class (:class:`~pulpcore.plugin.download.BaseDownloader`): The download
                class to be instantiated.
            url (str): The download URL.
            kwargs (dict): All kwargs are passed along to the downloader. At a minimum, these
                include the :class:`~pulpcore.plugin.download.BaseDownloader` parameters.

        Returns:
            :class:`~pulpcore.plugin.download.HttpDownloader`: A downloader that
            is configured with the importer settings.
        """
        options = {'session': self._session}
        if self._importer.proxy_url:
            options['proxy'] = self._importer.proxy_url

        return download_class(url, **options, **kwargs)

    def _generic(self, download_class, url, **kwargs):
        """
        Build a generic downloader based on the url.

        Args:
            download_class (:class:`~pulpcore.plugin.download.BaseDownloader`): The download
                class to be instantiated.
            url (str): The download URL.
            kwargs (dict): All kwargs are passed along to the downloader. At a minimum, these
                include the :class:`~pulpcore.plugin.download.BaseDownloader` parameters.

        Returns:
            subclass of :class:`~pulpcore.plugin.download.BaseDownloader`: A downloader that
            is configured with the importer settings.
        """
        return download_class(url, **kwargs)
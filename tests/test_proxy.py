import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from solvers.common.browser import _normalize_proxy
from solvers.common.proxypool import ProxyPool, _hostport, _mask, _normalize, _tcp_ok


class ProxyTests(unittest.TestCase):
    def test_normalization(self):
        cases = {
            '192.0.2.1:8080': 'http://192.0.2.1:8080',
            '[2001:db8::1]:8080': 'http://[2001:db8::1]:8080',
            'https://user@[2001:db8::1]:443': 'https://user@[2001:db8::1]:443',
            'http://user:@proxy.example:80': 'http://user:@proxy.example:80',
            'http://:secret@proxy.example:80': 'http://:secret@proxy.example:80',
            'socks5://u%40name:p%3Ass@[2001:db8::1]:1080':
                'socks5://u%40name:p%3Ass@[2001:db8::1]:1080',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalize(raw), expected)
                self.assertEqual(_normalize(expected), expected)

    def test_invalid_addresses(self):
        for raw in ('', '# comment', 'http://host', 'host:invalid',
                    '[2001:db8::1:80', 'host:65536'):
            with self.subTest(raw=raw):
                self.assertIsNone(_normalize(raw))

    def test_browser_credentials(self):
        self.assertEqual(
            _normalize_proxy('http://u%40name:p%3Ass@[2001:db8::1]:8080'),
            {'server': 'http://[2001:db8::1]:8080',
             'username': 'u@name', 'password': 'p:ss'},
        )
        self.assertEqual(_normalize_proxy('http://user@proxy.example:80')['password'], '')

    def test_browser_passthrough(self):
        for proxy in ('http://[2001:db8::1]:8080', 'http://192.0.2.1:80',
                      {'server': 'http://proxy.example:80'}):
            with self.subTest(proxy=proxy):
                self.assertEqual(_normalize_proxy(proxy), proxy)

    def test_hostport(self):
        for raw in ('http://user:secret@[2001:db8::1]:80', '[2001:db8::1]:80'):
            self.assertEqual(_hostport(raw), '[2001:db8::1]:80')
        self.assertEqual(_hostport('192.0.2.1:80'), '192.0.2.1:80')

    def test_mask(self):
        self.assertEqual(_mask('http://user:secret@[2001:db8::1]:80'),
                         'http://user:***@[2001:db8::1]:80')
        self.assertEqual(_mask('http://:secret@proxy.example:80'),
                         'http://:***@proxy.example:80')

    def test_pool_persistence_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'proxies.txt'
            pool = ProxyPool(path)
            proxy = 'http://user@[2001:db8::1]:80'
            self.assertEqual(pool.add([proxy, proxy]), (1, 1))
            restored = ProxyPool(path)
            acquired = restored.acquire()
            self.assertEqual(acquired.url, proxy)
            self.assertEqual(acquired.hostport, '[2001:db8::1]:80')
            self.assertTrue(restored.remove('[2001:db8::1]:80'))
            self.assertEqual(path.read_text().strip(), '')


class ProxyConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_connection(self):
        for address, host in (('[2001:db8::1]:8080', '2001:db8::1'),
                              ('192.0.2.1:8080', '192.0.2.1')):
            with self.subTest(address=address):
                writer = Mock()
                writer.wait_closed = AsyncMock()
                with patch('asyncio.open_connection', new_callable=AsyncMock) as connect:
                    connect.return_value = (object(), writer)
                    self.assertTrue(await _tcp_ok(address))
                    connect.assert_awaited_once_with(host, 8080)
                    writer.close.assert_called_once_with()
                    writer.wait_closed.assert_awaited_once_with()

    async def test_tcp_failure(self):
        with patch('asyncio.open_connection', new_callable=AsyncMock,
                   side_effect=OSError('unreachable')):
            self.assertFalse(await _tcp_ok('[2001:db8::1]:8080'))


if __name__ == '__main__':
    unittest.main()

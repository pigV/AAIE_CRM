import unittest

try:
    from tools import wechat_contract_demo as demo
except ImportError:
    demo = None


class DemoBoundaryTests(unittest.TestCase):
    def test_demo_boundary_is_available(self):
        self.assertIsNotNone(demo, 'Dedicated demo route boundary is not implemented')

    def test_only_known_tokens_and_exact_contract_routes_are_exposed(self):
        if demo is None:
            self.skipTest('Boundary not implemented')
        token = 'T' * 43
        tokens = {token}
        for suffix in ('', '/login', '/submit', '/final/view', '/final/download'):
            self.assertEqual(demo.route('/contract-sign/' + token + suffix, tokens), (token, suffix))
        for path in ('/', '/login', '/students', '/health', '/assets/seal.png',
                     '/contract-sign/unknown', '/contract-sign/' + token + '/other',
                     '/contract-sign/' + token + '/../login', '/contract-sign/' + token + '?x=1',
                     '/contract-sign/%54' + token[1:], '//contract-sign/' + token):
            with self.subTest(path=path):
                self.assertIsNone(demo.route(path, tokens))

    def test_test_identity_never_claims_real_wechat_verification(self):
        if demo is None:
            self.skipTest('Boundary not implemented')
        html = demo.label_demo('<body>微信身份已验证；<th>微信身份</th></body>')
        self.assertNotIn('微信身份已验证', html)
        self.assertIn('模拟家长身份', html)
        self.assertIn('测试合同', html)
        self.assertIn('不具法律效力', html)


if __name__ == '__main__':
    unittest.main()

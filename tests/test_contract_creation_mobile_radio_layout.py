import unittest
from pathlib import Path


class ContractCreationMobileRadioLayoutTests(unittest.TestCase):
    def test_scoped_radio_override_defeats_global_mobile_input_width(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        marker = "/* Premium V2 Contract Creation Radio Options */"
        end_marker = "/* End Premium V2 Contract Creation Radio Options */"
        self.assertIn(marker, css)
        self.assertIn(end_marker, css)
        scoped = css.split(marker, 1)[1].split(end_marker, 1)[0]

        for group in (".contract-type-choices", ".contract-signer-choices"):
            self.assertIn(
                f'{group} .choice > input[type="radio"]',
                scoped,
            )
            self.assertIn(f"{group} .choice > span", scoped)

        for declaration in (
            "flex: 0 0 18px;",
            "width: 18px !important;",
            "min-width: 18px !important;",
            "max-width: 18px !important;",
            "flex: 1 1 auto;",
            "min-width: 0;",
            "writing-mode: horizontal-tb;",
            "white-space: normal;",
            "overflow-wrap: break-word;",
            "word-break: normal;",
        ):
            self.assertIn(declaration, scoped)

        self.assertIn("@media (max-width: 760px)", scoped)
        self.assertNotIn("height: 100%", scoped)
        self.assertNotIn("min-height: 100px", scoped)

    def test_contract_markup_keeps_radio_inside_clickable_labels(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn('class="choices contract-type-choices"', source)
        self.assertIn('class="choices contract-signer-choices"', source)
        self.assertEqual(source.count('data-contract-signer-source><span>母亲</span>'), 1)
        self.assertEqual(source.count('data-contract-signer-source><span>父亲</span>'), 1)
        self.assertIn('class="choice wide contract-type-choice"><input type="radio"', source)


if __name__ == "__main__":
    unittest.main()

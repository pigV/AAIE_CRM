import unittest

from scripts import repair_failed_school_coordinates as repair


class SchoolGeocodeRepairTests(unittest.TestCase):
    def test_query_variants_expand_intersections_and_split_multi_campus_addresses(self):
        row = {
            "school_name": "Carranballac P-9 College",
            "state": "VIC",
            "suburb": "Point Cook | Point Cook",
            "postcode": "3030 | 3030",
            "address": "Cnr Foxwood Dve & Dunnings Rd, Point Cook, VIC 3030 | Cnr Jamieson Way & La Rochelle, Point Cook, VIC 3030",
        }

        variants = repair.build_query_variants(row)

        self.assertIn("Carranballac P-9 College, Point Cook VIC 3030, Australia", variants)
        self.assertTrue(any("Corner Foxwood Drive and Dunnings Road" in item for item in variants))
        self.assertTrue(any("Corner Jamieson Way and La Rochelle" in item for item in variants))

    def test_candidate_inside_act_bounds_is_accepted_when_state_text_is_missing(self):
        row = {
            "school_name": "Alfred Deakin High School",
            "state": "ACT",
            "suburb": "Deakin",
            "postcode": "2600",
            "address": "Denison Street",
        }
        candidate = {
            "lat": "-35.315",
            "lon": "149.109",
            "display_name": "Alfred Deakin High School, Denison Street, Deakin, Canberra, 2600, Australia",
            "address": {"postcode": "2600", "suburb": "Deakin"},
        }

        accepted = repair.candidate_from_nominatim(row, candidate)

        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["state"], "ACT")

    def test_candidate_with_matching_school_name_can_have_nearby_postcode_difference(self):
        row = {
            "school_name": "Strathfield South High School",
            "state": "NSW",
            "suburb": "Enfield",
            "postcode": "2136",
            "address": "Hedges Avenue",
        }
        candidate = {
            "lat": "-33.8853971",
            "lon": "151.0710100",
            "display_name": "Strathfield South High School, Hedges Avenue, Strathfield, Sydney, New South Wales, 2135, Australia",
            "address": {"postcode": "2135", "suburb": "Strathfield", "state": "New South Wales"},
        }

        accepted = repair.candidate_from_nominatim(row, candidate)

        self.assertIsNotNone(accepted)

    def test_candidate_rejects_similar_school_name_without_location_context(self):
        row = {
            "school_name": "Macquarie Grammar School",
            "state": "NSW",
            "suburb": "Sydney",
            "postcode": "2000",
            "address": "Level 8, 225 Clarence Street, Sydney NSW 2000",
        }
        candidate = {
            "lat": "-32.2487658",
            "lon": "148.5745049",
            "display_name": "Macquarie Anglican Grammar School, Road 17, Churchill Gardens, Dubbo, New South Wales, 2830, Australia",
            "address": {"postcode": "2830", "suburb": "Dubbo", "state": "New South Wales"},
        }

        accepted = repair.candidate_from_nominatim(row, candidate)

        self.assertIsNone(accepted)

    def test_photon_candidate_accepts_matching_school_and_street(self):
        row = {
            "school_name": "Cleveland State School",
            "state": "QLD",
            "suburb": "Cleveland",
            "postcode": "4163",
            "address": "Cnr Queen and Wynyard Streets, Cleveland QLD",
        }
        candidate = {
            "geometry": {"coordinates": [153.2704451, -27.5291931]},
            "properties": {
                "name": "Cleveland State Primary School",
                "street": "Wynyard Street",
                "city": "Cleveland",
                "state": "Queensland",
                "postcode": "4163",
                "country": "Australia",
            },
        }

        accepted = repair.candidate_from_photon(row, candidate)

        self.assertIsNotNone(accepted)

    def test_photon_candidate_rejects_different_school_on_same_street(self):
        row = {
            "school_name": "Meadow Springs Primary School",
            "state": "WA",
            "suburb": "Meadow Springs",
            "postcode": "6210",
            "address": "320 Oakmont Avenue, Meadow Springs, WA, 6210",
        }
        candidate = {
            "geometry": {"coordinates": [115.7464477, -32.5100602]},
            "properties": {
                "name": "Assumption Catholic Primary School",
                "street": "Oakmont Avenue",
                "city": "Mandurah",
                "state": "Western Australia",
                "postcode": "6210",
                "country": "Australia",
            },
        }

        accepted = repair.candidate_from_photon(row, candidate)

        self.assertIsNone(accepted)

    def test_photon_candidate_rejects_similar_school_without_phrase_or_address(self):
        row = {
            "school_name": "Macquarie Grammar School",
            "state": "NSW",
            "suburb": "Sydney",
            "postcode": "2000",
            "address": "Level 8, 225 Clarence Street, Sydney NSW 2000",
        }
        candidate = {
            "geometry": {"coordinates": [151.2133705, -33.8749917]},
            "properties": {
                "name": "Sydney Grammar School",
                "street": "College Lane",
                "city": "Sydney",
                "state": "New South Wales",
                "postcode": "2010",
                "country": "Australia",
            },
        }

        accepted = repair.candidate_from_photon(row, candidate)

        self.assertIsNone(accepted)

    def test_extract_google_embed_coordinates(self):
        html = '<iframe src="https://www.google.com/maps/embed?pb=!2d152.83919171540623!3d-28.09023068263487"></iframe>'

        self.assertEqual(repair.extract_google_embed_coordinates(html), [(-28.09023068263487, 152.83919171540623)])


if __name__ == "__main__":
    unittest.main()

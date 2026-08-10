import unittest

from clipper_app.application.hashtag_relevance import (
    HashtagRelevanceClassifier,
    filter_relevant_hashtags,
)


class HashtagRelevanceClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = HashtagRelevanceClassifier()

    def test_classifies_core_topics_concerns_ingredients_products_and_treatments(self):
        expected_categories = {
            "#skincare": "topic",
            "#darkspots": "skin_concern",
            "#acne": "skin_concern",
            "#vitamincserum": "ingredient",
            "#sunscreen": "product",
            "#makeup": "topic",
            "#personalcare": "topic",
            "#NightTimeRoutine": "routine",
            "#microneedling": "treatment",
            "#bekasjerawat": "skin_concern",
        }

        for hashtag, category in expected_categories.items():
            with self.subTest(hashtag=hashtag):
                result = self.classifier.classify(hashtag)
                self.assertTrue(result.relevant)
                self.assertEqual(result.category, category)

    def test_classifies_brand_names_and_supported_variations_without_broad_substrings(self):
        for hashtag in (
            "#skintific",
            "#skintificindonesia",
            "#skintific_id",
            "#skintific-official",
            "#glad2glow",
            "#glad2glowindonesia",
        ):
            with self.subTest(hashtag=hashtag):
                result = self.classifier.classify(hashtag)
                self.assertTrue(result.relevant)
                self.assertEqual(result.category, "beauty_brand")
                self.assertIn(result.matched_brand, {"skintific", "glad2glow"})

        for hashtag in ("#appleofficial", "#microsoft", "#skintificator"):
            with self.subTest(hashtag=hashtag):
                self.assertFalse(self.classifier.classify(hashtag).relevant)

    def test_assigns_exclusion_category_or_unclassified_reason(self):
        expected_categories = {
            "#MinecraftGameplay": "gaming",
            "#WorldCupFootball": "sports",
            "#ElectionNews": "politics",
            "#MovieReview": "entertainment",
            "#EasyDinnerRecipe": "food",
            "#Supercar": "vehicles",
            "#FunnyMeme": "general_meme",
            "#TodayInMyCity": "unclassified",
        }

        for hashtag, category in expected_categories.items():
            with self.subTest(hashtag=hashtag):
                result = self.classifier.classify(hashtag)
                self.assertFalse(result.relevant)
                self.assertEqual(result.category, category)
                self.assertTrue(result.reason)

    def test_filter_preserves_tiktok_rank_caps_at_thirty_and_does_not_backfill(self):
        source = [
            {"hashtag_id": "food", "hashtag_name": "recipe", "rank_position": 1},
            *[
                {
                    "hashtag_id": f"skin-{rank}",
                    "hashtag_name": f"skincare{rank}",
                    "rank_position": rank,
                }
                for rank in range(2, 34)
            ],
            {"hashtag_id": "game", "hashtag_name": "gaming", "rank_position": 34},
        ]

        result = filter_relevant_hashtags(reversed(source), limit=30)

        self.assertEqual(result.total_count, 34)
        self.assertEqual(result.relevant_count, 32)
        self.assertEqual(result.excluded_count, 2)
        self.assertEqual(len(result.selected), 30)
        self.assertEqual([item["rank_position"] for item in result.selected], list(range(2, 32)))
        self.assertEqual([item["display_rank"] for item in result.selected], list(range(1, 31)))

        sparse = filter_relevant_hashtags(source[:3], limit=30)
        self.assertEqual(len(sparse.selected), 2)
        self.assertNotIn("food", {item["hashtag_id"] for item in sparse.selected})

    def test_deduplicates_across_sources_and_keeps_primary_tiktok_rank_order(self):
        source = [
            {
                "hashtag_id": "supplemental-duplicate",
                "hashtag_name": "Skin-tific",
                "rank_position": 1,
                "source": "supplemental",
                "source_category": "SEARCH",
            },
            {
                "hashtag_id": "primary-topic",
                "hashtag_name": "skincare",
                "rank_position": 2,
                "source": "tiktok_discovery_trending",
                "source_category": "BEAUTY_AND_PERSONAL_CARE",
            },
            {
                "hashtag_id": "primary-brand",
                "hashtag_name": "skintific",
                "rank_position": 10,
                "source": "tiktok_discovery_trending",
                "source_category": "BEAUTY_AND_PERSONAL_CARE",
            },
        ]

        result = filter_relevant_hashtags(
            source,
            default_source_category="BEAUTY_AND_PERSONAL_CARE",
        )

        self.assertEqual(
            [item["hashtag_id"] for item in result.selected],
            ["primary-topic", "primary-brand"],
        )
        self.assertEqual(result.deduplicated_count, 1)
        self.assertEqual([item["original_rank"] for item in result.selected], [2, 10])

    def test_logs_summary_and_a_reason_for_every_exclusion(self):
        source = [
            {"hashtag_name": "acne", "rank_position": 1},
            {"hashtag_name": "football", "rank_position": 2},
            {"hashtag_name": "mysterytopic", "rank_position": 3},
        ]

        with self.assertLogs("clipper_app.application.hashtag_relevance", level="DEBUG") as captured:
            filter_relevant_hashtags(source)

        output = "\n".join(captured.output)
        self.assertIn("retrieved=3 topical=1 brands=0 excluded=2 deduplicated=0 returned=1", output)
        self.assertIn("hashtag=#football normalized=football", output)
        self.assertIn("rank=2 relevant=False category=sports", output)
        self.assertIn("hashtag=#mysterytopic normalized=mysterytopic", output)
        self.assertIn("rank=3 relevant=False category=unclassified", output)


if __name__ == "__main__":
    unittest.main()

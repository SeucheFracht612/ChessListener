#!/usr/bin/env python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import review

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class ReviewTests(unittest.TestCase):
    def test_loss_is_mover_relative(self):
        self.assertEqual(review.evaluation_loss({"cp": 50}, {"cp": -50}, "w"), 100)
        self.assertEqual(review.evaluation_loss({"cp": -20}, {"cp": 80}, "b"), 100)

    def test_conservative_classifications(self):
        self.assertEqual(review.classify_move(0, "a", "b"), "Best")
        self.assertEqual(review.classify_move(70, "a", "b"), "Good")
        self.assertEqual(review.classify_move(301, "a", "b"), "Blunder")
        self.assertEqual(review.classify_move(999, "a", "a"), "Best")

    def test_pgn_replays_and_annotates(self):
        moves = ["e2e4", "e7e5", "g1f3"]
        items = [
            {"classification": "Best", "loss": 0, "eval": "+0.30", "best": move}
            for move in moves
        ]
        text = review.annotated_pgn(START, moves, items, "*")
        self.assertIn("1. e4", text)
        self.assertIn("e5", text)
        self.assertIn("2. Nf3", text)
        self.assertIn("{Best; loss 0.00; eval +0.30}", text)

    def test_separate_uci_job_reports_progress_and_completion(self):
        fake = os.path.join(os.path.dirname(__file__), "fake_uci_engine.py")
        job, output = review.start_review(
            START, ["e2e4", "e7e5"],
            {"engine": fake, "time_ms": 25, "lines": 2, "threads": 1},
        )
        job.join(5)
        self.assertFalse(job.is_alive())
        messages = []
        while not output.empty():
            messages.append(output.get())
        self.assertEqual(messages[-1]["type"], "complete")
        self.assertEqual(len(messages[-1]["reviews"]), 2)
        self.assertEqual(len(messages[-1]["position_analyses"]), 3)
        self.assertEqual(
            [message["type"] for message in messages[:-1]],
            ["progress", "progress", "progress"],
        )

    def test_zero_move_fen_is_analysed_as_a_position(self):
        fake = os.path.join(os.path.dirname(__file__), "fake_uci_engine.py")
        job, output = review.start_review(
            START, [], {"engine": fake, "time_ms": 25, "lines": 2, "threads": 1}
        )
        job.join(5)
        self.assertFalse(job.is_alive())
        messages = []
        while not output.empty():
            messages.append(output.get())
        self.assertEqual([item["type"] for item in messages], ["progress", "complete"])
        self.assertEqual(messages[-1]["reviews"], [])
        self.assertEqual(messages[-1]["positions"], [START])
        self.assertEqual(len(messages[-1]["position_analyses"]), 1)

    def test_historical_explorer_position_search_is_generation_tagged(self):
        fake = os.path.join(os.path.dirname(__file__), "fake_uci_engine.py")
        job, output = review.start_position_analysis(
            START, {"engine": fake, "time_ms": 25, "lines": 2, "threads": 1}, 17
        )
        job.join(5)
        self.assertFalse(job.is_alive())
        message = output.get_nowait()
        self.assertEqual(message["type"], "position_complete")
        self.assertEqual(message["generation"], 17)
        self.assertEqual(len(message["lines"]), 2)


if __name__ == "__main__":
    unittest.main()

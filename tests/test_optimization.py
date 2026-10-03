"""Tests for the automatic tile size search (needs numpy only, not QGIS)"""
import os
import sys
import tracemalloc
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from optimization import optimize_dtb, optimize_fac

MAX_PIX = 1024 * 1024


def tiles(x, y, w, h):
    return -(-x // int(w)) * -(-y // int(h))


class OptimizationTest(unittest.TestCase):

    def test_small_image_is_one_tile(self):
        self.assertEqual(optimize_fac(800, 600, MAX_PIX, 100), (800, 600))

    def test_constraints_are_kept(self):
        for x, y in [(1920, 1080), (4001, 3001), (7681, 4321), (2053, 1031)]:
            for max_tiles in (100, 500):
                w, h = optimize_fac(x, y, MAX_PIX, max_tiles)
                self.assertLessEqual(int(w) * int(h), MAX_PIX, (x, y))
                self.assertLessEqual(tiles(x, y, w, h), max_tiles, (x, y))

    def test_impossible_constraints(self):
        # 200 megapixels can never fit into 100 tiles of 1 megapixel
        self.assertEqual(optimize_fac(20011, 10007, MAX_PIX, 100), (1, 1))

    def test_large_awkward_image_needs_little_memory(self):
        # Prime-ish sizes have no useful factors, which used to trigger a
        # search over every width and height (1.4 GB for this size)
        tracemalloc.start()
        try:
            w, h = optimize_dtb(7681, 4321, MAX_PIX, 100)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 50 * 1024 * 1024)
        self.assertLessEqual(int(w) * int(h), MAX_PIX)
        self.assertLessEqual(tiles(7681, 4321, w, h), 100)


if __name__ == '__main__':
    unittest.main()

"""64×64 块文件格式与原子存储测试。"""
import os
import unittest

from elevation.blockstore import (BlockFormatError, BlockStore, decode_block,
                                  encode_block, is_block_filename)
from tests.helpers import make_config


class BlockCodecTest(unittest.TestCase):
    def test_roundtrip(self):
        cells = {(0, 0): 12, (63, 63): -7, (10, 20): 2 ** 31 - 1}
        data = encode_block(2, 3, cells)
        decoded = decode_block(data, expect_br=2, expect_bc=3)
        self.assertEqual(decoded, cells)

    def test_zero_cell_encoded_explicitly(self):
        cells = {(0, 1): 0}
        decoded = decode_block(encode_block(0, 0, cells))
        self.assertEqual(decoded, {(0, 1): 0})  # 显式写入的 0 与未填格可区分

    def test_crc_corruption_detected(self):
        data = bytearray(encode_block(0, 0, {(1, 1): 5}))
        data[20] ^= 0xFF
        with self.assertRaises(BlockFormatError):
            decode_block(bytes(data))

    def test_wrong_block_coords_rejected(self):
        data = encode_block(1, 2, {(1, 1): 5})
        with self.assertRaises(BlockFormatError):
            decode_block(data, expect_br=1, expect_bc=3)

    def test_int32_bounds(self):
        with self.assertRaises(ValueError):
            encode_block(0, 0, {(0, 0): 2 ** 31})
        with self.assertRaises(ValueError):
            encode_block(0, 0, {(0, 0): -(2 ** 31) - 1})


class BlockStoreTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.store = BlockStore(self.cfg)

    def tearDown(self):
        from tests.helpers import cleanup_config
        cleanup_config(self.cfg)

    def test_write_is_immutable_new_file(self):
        name1 = self.store.write_block(0, 0, {(0, 0): 1})
        name2 = self.store.write_block(0, 0, {(0, 0): 1, (0, 1): 2})
        self.assertNotEqual(name1, name2)
        self.assertTrue(os.path.exists(self.store.block_path(name1)))
        self.assertTrue(os.path.exists(self.store.block_path(name2)))
        self.assertEqual(self.store.read_block(0, 0, name2)[(0, 1)], 2)

    def test_filename_pattern(self):
        name = self.store.write_block(5, 7, {(0, 0): 1})
        self.assertTrue(is_block_filename(name))
        self.assertFalse(is_block_filename("random.txt"))
        self.assertFalse(is_block_filename("b0-0-short.blk"))

    def test_tmp_cleanup(self):
        path = os.path.join(self.cfg.tmp_dir, "b0-0-x.tmp")
        with open(path, "wb") as fh:
            fh.write(b"partial")
        removed = self.store.cleanup_tmp()
        self.assertEqual(removed, ["b0-0-x.tmp"])
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()

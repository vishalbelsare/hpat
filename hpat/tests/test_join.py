import unittest
import pandas as pd
import numpy as np
import random
import string
import pyarrow.parquet as pq
import numba
import hpat
from hpat import hiframes_sort
from hpat.str_arr_ext import StringArray
from hpat.tests.test_utils import (count_array_REPs, count_parfor_REPs,
                            count_parfor_OneDs, count_array_OneDs, dist_IR_contains,
                            get_start_end)


class TestJoin(unittest.TestCase):
    def test_join1(self):
        def test_impl(n):
            df1 = pd.DataFrame({'key1': np.arange(n)+3, 'A': np.arange(n)+1.0})
            df2 = pd.DataFrame({'key2': 2*np.arange(n)+1, 'B': n+np.arange(n)+1.0})
            df3 = pd.merge(df1, df2, left_on='key1', right_on='key2')
            return df3.B.sum()

        hpat_func = hpat.jit(test_impl)
        n = 11
        self.assertEqual(hpat_func(n), test_impl(n))
        self.assertEqual(count_array_REPs(), 0)
        self.assertEqual(count_parfor_REPs(), 0)
        n = 11111
        self.assertEqual(hpat_func(n), test_impl(n))

    def test_join1_seq(self):
        def test_impl(n):
            df1 = pd.DataFrame({'key1': np.arange(n)+3, 'A': np.arange(n)+1.0})
            df2 = pd.DataFrame({'key2': 2*np.arange(n)+1, 'B': n+np.arange(n)+1.0})
            df3 = df1.merge(df2, left_on='key1', right_on='key2')
            return df3.B

        hpat_func = hpat.jit(test_impl)
        n = 11
        self.assertEqual(hpat_func(n).sum(), test_impl(n).sum())
        self.assertEqual(count_array_OneDs(), 0)
        self.assertEqual(count_parfor_OneDs(), 0)
        n = 11111
        self.assertEqual(hpat_func(n).sum(), test_impl(n).sum())

    def test_join1_seq_str(self):
        def test_impl():
            df1 = pd.DataFrame({'key1': ['foo', 'bar', 'baz']})
            df2 = pd.DataFrame({'key2': ['baz', 'bar', 'baz'], 'B': ['b', 'zzz', 'ss']})
            df3 = pd.merge(df1, df2, left_on='key1', right_on='key2')
            return df3.B

        hpat_func = hpat.jit(test_impl)
        self.assertEqual(set(hpat_func()), set(test_impl()))

    def test_join1_seq_str_na(self):
        # test setting NA in string data column
        def test_impl():
            df1 = pd.DataFrame({'key1': ['foo', 'bar', 'baz']})
            df2 = pd.DataFrame({'key2': ['baz', 'bar', 'baz'], 'B': ['b', 'zzz', 'ss']})
            df3 = df1.merge(df2, left_on='key1', right_on='key2', how='left')
            return df3.B

        hpat_func = hpat.jit(test_impl)
        self.assertEqual(set(hpat_func()), set(test_impl()))

    def test_join_mutil_seq1(self):
        def test_impl(df1, df2):
            return df1.merge(df2, on=['A', 'B'])

        hpat_func = hpat.jit(test_impl)
        df1 = pd.DataFrame({'A': [3,1,1,3,4],
                            'B': [1,2,3,2,3],
                            'C': [7,8,9,4,5]})

        df2 = pd.DataFrame({'A': [2,1,4,4,3],
                            'B': [1,3,2,3,2],
                            'D': [1,2,3,4,8]})

        pd.testing.assert_frame_equal(hpat_func(df1, df2), test_impl(df1, df2))

    def test_join_mutil_parallel1(self):
        def test_impl(A1, B1, C1, A2, B2, D2):
            df1 = pd.DataFrame({'A': A1, 'B': B1, 'C': C1})
            df2 = pd.DataFrame({'A': A2, 'B': B2, 'D': D2})
            df3 = df1.merge(df2, on=['A', 'B'])
            return df3.C.sum() + df3.D.sum()

        hpat_func = hpat.jit(locals={
            'A1:input': 'distributed',
            'B1:input': 'distributed',
            'C1:input': 'distributed',
            'A2:input': 'distributed',
            'B2:input': 'distributed',
            'D2:input': 'distributed',})(test_impl)
        df1 = pd.DataFrame({'A': [3,1,1,3,4],
                            'B': [1,2,3,2,3],
                            'C': [7,8,9,4,5]})

        df2 = pd.DataFrame({'A': [2,1,4,4,3],
                            'B': [1,3,2,3,2],
                            'D': [1,2,3,4,8]})

        start, end = get_start_end(len(df1))
        h_A1 = df1.A.values[start:end]
        h_B1 = df1.B.values[start:end]
        h_C1 = df1.C.values[start:end]
        h_A2 = df2.A.values[start:end]
        h_B2 = df2.B.values[start:end]
        h_D2 = df2.D.values[start:end]
        p_A1 = df1.A.values
        p_B1 = df1.B.values
        p_C1 = df1.C.values
        p_A2 = df2.A.values
        p_B2 = df2.B.values
        p_D2 = df2.D.values
        h_res = hpat_func(h_A1, h_B1, h_C1, h_A2, h_B2, h_D2)
        p_res = test_impl(p_A1, p_B1, p_C1, p_A2, p_B2, p_D2)
        self.assertEqual(h_res, p_res)

    def test_merge_asof_seq1(self):
        def test_impl(df1, df2):
            return pd.merge_asof(df1, df2, on='time')

        hpat_func = hpat.jit(test_impl)
        df1 = pd.DataFrame(
            {'time': pd.DatetimeIndex(
                ['2017-01-03', '2017-01-06', '2017-02-21']), 'B': [4, 5, 6]})
        df2 = pd.DataFrame(
            {'time': pd.DatetimeIndex(
                ['2017-01-01', '2017-01-02', '2017-01-04', '2017-02-23',
                '2017-02-25']), 'A': [2,3,7,8,9]})
        pd.testing.assert_frame_equal(hpat_func(df1, df2), test_impl(df1, df2))

    def test_merge_asof_parallel1(self):
        def test_impl():
            df1 = pd.read_parquet('asof1.pq')
            df2 = pd.read_parquet('asof2.pq')
            df3 = pd.merge_asof(df1, df2, on='time')
            return (df3.A.sum(), df3.time.max(), df3.B.sum())

        hpat_func = hpat.jit(test_impl)
        self.assertEqual(hpat_func(), test_impl())

    def test_join_left_seq1(self):
        def test_impl(df1, df2):
            return pd.merge(df1, df2, how='left', on='key')

        hpat_func = hpat.jit(test_impl)
        df1 = pd.DataFrame(
            {'key': [2,3,5,1,2,8], 'A': np.array([4,6,3,9,9,-1], np.float)})
        df2 = pd.DataFrame(
            {'key': [1,2,9,3,2], 'B': np.array([1,7,2,6,5], np.float)})
        h_res = hpat_func(df1, df2)
        res = test_impl(df1, df2)
        np.testing.assert_array_equal(h_res.key.values, res.key.values)
        # converting arrays to sets since order of values can be different
        self.assertEqual(set(h_res.A.values), set(res.A.values))
        self.assertEqual(
            set(h_res.B.dropna().values), set(res.B.dropna().values))

    def test_join_left_seq2(self):
        def test_impl(df1, df2):
            return pd.merge(df1, df2, how='left', on='key')

        hpat_func = hpat.jit(test_impl)
        # test left run where a key is repeated on left but not right side
        df1 = pd.DataFrame(
            {'key': [2,3,5,3,2,8], 'A': np.array([4,6,3,9,9,-1], np.float)})
        df2 = pd.DataFrame(
            {'key': [1,2,9,3,10], 'B': np.array([1,7,2,6,5], np.float)})
        h_res = hpat_func(df1, df2)
        res = test_impl(df1, df2)
        np.testing.assert_array_equal(h_res.key.values, res.key.values)
        # converting arrays to sets since order of values can be different
        self.assertEqual(set(h_res.A.values), set(res.A.values))
        self.assertEqual(
            set(h_res.B.dropna().values), set(res.B.dropna().values))

    def test_join_right_seq1(self):
        def test_impl(df1, df2):
            return pd.merge(df1, df2, how='right', on='key')

        hpat_func = hpat.jit(test_impl)
        df1 = pd.DataFrame(
            {'key': [2,3,5,1,2,8], 'A': np.array([4,6,3,9,9,-1], np.float)})
        df2 = pd.DataFrame(
            {'key': [1,2,9,3,2], 'B': np.array([1,7,2,6,5], np.float)})
        h_res = hpat_func(df1, df2)
        res = test_impl(df1, df2)
        self.assertEqual(set(h_res.key.values), set(res.key.values))
        # converting arrays to sets since order of values can be different
        self.assertEqual(set(h_res.B.values), set(res.B.values))
        self.assertEqual(
            set(h_res.A.dropna().values), set(res.A.dropna().values))

    def test_join_outer_seq1(self):
        def test_impl(df1, df2):
            return pd.merge(df1, df2, how='outer', on='key')

        hpat_func = hpat.jit(test_impl)
        df1 = pd.DataFrame(
            {'key': [2,3,5,1,2,8], 'A': np.array([4,6,3,9,9,-1], np.float)})
        df2 = pd.DataFrame(
            {'key': [1,2,9,3,2], 'B': np.array([1,7,2,6,5], np.float)})
        h_res = hpat_func(df1, df2)
        res = test_impl(df1, df2)
        self.assertEqual(set(h_res.key.values), set(res.key.values))
        # converting arrays to sets since order of values can be different
        self.assertEqual(
            set(h_res.B.dropna().values), set(res.B.dropna().values))
        self.assertEqual(
            set(h_res.A.dropna().values), set(res.A.dropna().values))



if __name__ == "__main__":
    unittest.main()

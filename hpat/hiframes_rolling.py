import numpy as np
import pandas as pd
import hpat
import numba
from numba import types
from numba.extending import lower_builtin
from numba.targets.imputils import impl_ret_new_ref, impl_ret_borrowed
from numba.typing import signature
from numba.typing.templates import infer_global, AbstractTemplate
from numba.ir_utils import guard, find_const

from hpat.distributed_api import Reduce_Type

def get_rolling_setup_args(func_ir, rhs, get_consts=True):
    """
    Handle Series rolling calls like:
        r = df.column.rolling(3)
    """
    center = False
    kws = dict(rhs.kws)
    if rhs.args:
        window = rhs.args[0]
    elif 'window' in kws:
        window = kws['window']
    else:  # pragma: no cover
        raise ValueError("window argument to rolling() required")
    if get_consts:
        window_const = guard(find_const, func_ir, window)
        window = window_const if window_const is not None else window
    if 'center' in kws:
        center = kws['center']
        if get_consts:
            center_const = guard(find_const, func_ir, center)
            center = center_const if center_const is not None else center
    return window, center


def rolling_fixed(arr, win):  # pragma: no cover
    return arr

def rolling_fixed_parallel(arr, win):  # pragma: no cover
    return arr


@infer_global(rolling_fixed)
@infer_global(rolling_fixed_parallel)
class RollingType(AbstractTemplate):
    def generic(self, args, kws):
        arr = args[0]  # array or series
        # result is always float64 in pandas
        # see _prep_values() in window.py
        return signature(arr.copy(dtype=types.float64), arr, types.intp,
                         types.bool_, types.bool_, args[4])

RollingType.support_literals = True


@lower_builtin(rolling_fixed, types.Array, types.Integer, types.Boolean,
               types.Boolean, types.Const)
def lower_rolling_fixed(context, builder, sig, args):
    func_name = sig.args[-1].value
    if func_name == 'sum':
        func = roll_sum_fixed

    res = context.compile_internal(
        builder, func, signature(sig.return_type, *sig.args[:-1]), args[:-1])
    return impl_ret_borrowed(context, builder, sig.return_type, res)


#### adapted from pandas window.pyx ####

comm_border_tag = 22  # arbitrary, TODO: revisit comm tags

def roll_sum_fixed(in_arr, win, center, parallel):
    rank = hpat.distributed_api.get_rank()
    n_pes = hpat.distributed_api.get_size()
    N = len(in_arr)
    # TODO: support minp arg end_range etc.
    minp = win
    offset = (win - 1) // 2 if center else 0

    if parallel:
        # halo length is w/2 to handle even w such as w=4
        halo_size = np.int32(win // 2) if center else np.int32(win-1)
        if _is_small_for_parallel(N, halo_size):
            return _handle_small_data(in_arr, win, center, rank, n_pes)

        comm_data = _border_icomm(
            in_arr, rank, n_pes, halo_size, in_arr.dtype, center)
        (l_recv_buff, r_recv_buff, l_send_req, r_send_req, l_recv_req,
            r_recv_req) = comm_data

    output, nobs, sum_x = roll_sum_fixed_seq(in_arr, win, center)

    if parallel:
        _border_send_wait(r_send_req, l_send_req, rank, n_pes, center)

        # recv right
        if center and rank != n_pes - 1:
            hpat.distributed_api.wait(r_recv_req, True)

            for i in range(0, halo_size):
                nobs, sum_x = add_sum(r_recv_buff[i], nobs, sum_x)

                prev_x = in_arr[N + i - win]
                nobs, sum_x = remove_sum(prev_x, nobs, sum_x)

                output[N + i - offset] = calc_sum(minp, nobs, sum_x)

        # recv left
        if rank != 0:
            hpat.distributed_api.wait(l_recv_req, True)
            sum_x = 0.0
            nobs = 0
            for i in range(0, halo_size):
                nobs, sum_x = add_sum(l_recv_buff[i], nobs, sum_x)

            for i in range(0, win - 1):
                nobs, sum_x = add_sum(in_arr[i], nobs, sum_x)

                if i > offset:
                    prev_x = l_recv_buff[i - offset - 1]
                    nobs, sum_x = remove_sum(prev_x, nobs, sum_x)

                if i >= offset:
                    output[i - offset] = calc_sum(minp, nobs, sum_x)

    return output

@numba.njit
def roll_sum_fixed_seq(in_arr, win, center):
    sum_x = 0.0
    nobs = 0
    N = len(in_arr)
    # TODO: support minp arg end_range etc.
    minp = win
    offset = (win - 1) // 2 if center else 0
    output = np.empty(N, dtype=np.float64)
    range_endpoint = max(minp, 1) - 1
    # in case window is smaller than array
    range_endpoint = min(range_endpoint, N)

    for i in range(0, range_endpoint):
        nobs, sum_x = add_sum(in_arr[i], nobs, sum_x)
        if i >= offset:
            output[i - offset] = np.nan

    for i in range(range_endpoint, N):
        val = in_arr[i]
        nobs, sum_x = add_sum(val, nobs, sum_x)

        if i > win - 1:
            prev_x = in_arr[i - win]
            nobs, sum_x = remove_sum(prev_x, nobs, sum_x)

        output[i - offset] = calc_sum(minp, nobs, sum_x)

    for j in range(N - offset, N):
        output[j] = np.nan

    return output, nobs, sum_x

@numba.njit
def add_sum(val, nobs, sum_x):
    if not np.isnan(val):
        nobs += 1
        sum_x += val
    return nobs, sum_x

@numba.njit
def remove_sum(val, nobs, sum_x):
    if not np.isnan(val):
        nobs -= 1
        sum_x -= val
    return nobs, sum_x

@numba.njit
def calc_sum(minp, nobs, sum_x):
    return sum_x if nobs >= minp else np.nan

@numba.njit
def _border_icomm(in_arr, rank, n_pes, halo_size, dtype, center):
    comm_tag = np.int32(comm_border_tag)
    l_recv_buff = np.empty(halo_size, dtype)
    if center:
        r_recv_buff = np.empty(halo_size, dtype)
    # send right
    if rank != n_pes - 1:
        r_send_req = hpat.distributed_api.isend(in_arr[-halo_size:], halo_size, np.int32(rank+1), comm_tag, True)
    # recv left
    if rank != 0:
        l_recv_req = hpat.distributed_api.irecv(l_recv_buff, halo_size, np.int32(rank-1), comm_tag, True)
    # center cases
    # send left
    if center and rank != 0:
        l_send_req = hpat.distributed_api.isend(in_arr[:halo_size], halo_size, np.int32(rank-1), comm_tag, True)
    # recv right
    if center and rank != n_pes - 1:
        r_recv_req = hpat.distributed_api.irecv(r_recv_buff, halo_size, np.int32(rank+1), comm_tag, True)

    return l_recv_buff, r_recv_buff, l_send_req, r_send_req, l_recv_req, r_recv_req

@numba.njit
def _border_send_wait(r_send_req, l_send_req, rank, n_pes, center):
    # wait on send right
    if rank != n_pes - 1:
        hpat.distributed_api.wait(r_send_req, True)
    # wait on send left
    if center and rank != 0:
        hpat.distributed_api.wait(l_send_req, True)

@numba.njit
def _is_small_for_parallel(N, halo_size):
    # gather data on one processor and compute sequentially if data of any
    # processor is too small for halo size
    # TODO: handle 1D_Var or other cases where data is actually large but
    # highly imbalanced
    # TODO: avoid reduce for obvious cases like no center and large 1D_Block
    num_small = hpat.distributed_api.dist_reduce(
        int(N<halo_size), np.int32(Reduce_Type.Sum.value))
    return num_small != 0

@numba.njit
def _handle_small_data(in_arr, win, center, rank, n_pes):
    all_N = hpat.distributed_api.dist_reduce(
        len(in_arr), np.int32(Reduce_Type.Sum.value))
    all_in_arr = hpat.distributed_api.gatherv(in_arr)
    if rank == 0:
        all_out, _, _ = roll_sum_fixed_seq(all_in_arr, win, center)
    else:
        all_out = np.empty(all_N, np.float64)
    hpat.distributed_api.bcast(all_out)
    start = hpat.distributed_api.get_start(all_N, n_pes, rank)
    end = hpat.distributed_api.get_end(all_N, n_pes, rank)
    return all_out[start:end]
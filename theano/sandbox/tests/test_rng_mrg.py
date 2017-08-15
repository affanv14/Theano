from __future__ import absolute_import, print_function, division
import os
import sys
import time
import unittest

from nose.tools import assert_raises
import numpy as np
from six.moves import xrange

import theano
from theano import change_flags, config, tensor
from theano.sandbox import rng_mrg
from theano.sandbox.rng_mrg import MRG_RandomStreams
from theano.tests import unittest_tools as utt
from theano.tests.unittest_tools import attr

# TODO: test MRG_RandomStreams
# Partly done in test_consistency_randomstreams

# TODO: test optimizer mrg_random_make_inplace

utt.seed_rng()

# Results generated by Java code using L'Ecuyer et al.'s code, with:
# main seed: [12345]*6 (default)
# 12 streams
# 7 substreams for each stream
# 5 samples drawn from each substream
java_samples = np.loadtxt(os.path.join(os.path.split(theano.__file__)[0],
                                       'sandbox',
                                       'samples_MRG31k3p_12_7_5.txt'))


def test_deterministic():
    seed = utt.fetch_seed()
    sample_size = (10, 20)

    R = MRG_RandomStreams(seed=seed)
    u = R.uniform(size=sample_size)
    f = theano.function([], u)

    fsample1 = f()
    fsample2 = f()
    assert not np.allclose(fsample1, fsample2)

    R2 = MRG_RandomStreams(seed=seed)
    u2 = R2.uniform(size=sample_size)
    g = theano.function([], u2)
    gsample1 = g()
    gsample2 = g()
    assert np.allclose(fsample1, gsample1)
    assert np.allclose(fsample2, gsample2)


def test_consistency_randomstreams():
    # Verify that the random numbers generated by MRG_RandomStreams
    # are the same as the reference (Java) implementation by L'Ecuyer et al.
    seed = 12345
    n_samples = 5
    n_streams = 12
    n_substreams = 7

    samples = []
    rng = MRG_RandomStreams(seed=seed)
    for i in range(n_streams):
        stream_samples = []
        u = rng.uniform(size=(n_substreams,), nstreams=n_substreams)
        f = theano.function([], u)
        for j in range(n_samples):
            s = f()
            stream_samples.append(s)
        stream_samples = np.array(stream_samples)
        stream_samples = stream_samples.T.flatten()
        samples.append(stream_samples)

    samples = np.array(samples).flatten()
    assert(np.allclose(samples, java_samples))


def test_get_substream_rstates():
    try:
        orig = theano.config.compute_test_value
        theano.config.compute_test_value = 'raise'
        n_streams = 100

        dtype = 'float32'
        rng = MRG_RandomStreams(np.random.randint(2147462579))

        rng.get_substream_rstates(n_streams, dtype)

    finally:
        theano.config.compute_test_value = orig


def test_consistency_cpu_serial():
    """
    Verify that the random numbers generated by mrg_uniform, serially,
    are the same as the reference (Java) implementation by L'Ecuyer et al.

    """
    seed = 12345
    n_samples = 5
    n_streams = 12
    n_substreams = 7

    samples = []
    curr_rstate = np.array([seed] * 6, dtype='int32')

    for i in range(n_streams):
        stream_rstate = curr_rstate.copy()
        for j in range(n_substreams):
            rstate = theano.shared(np.array([stream_rstate.copy()],
                                            dtype='int32'))
            new_rstate, sample = rng_mrg.mrg_uniform.new(rstate, ndim=None,
                                                         dtype=config.floatX,
                                                         size=(1,))
            # Not really necessary, just mimicking
            # rng_mrg.MRG_RandomStreams' behavior
            sample.rstate = rstate
            sample.update = (rstate, new_rstate)

            rstate.default_update = new_rstate
            f = theano.function([], sample)
            for k in range(n_samples):
                s = f()
                samples.append(s)

            # next substream
            stream_rstate = rng_mrg.ff_2p72(stream_rstate)

        # next stream
        curr_rstate = rng_mrg.ff_2p134(curr_rstate)

    samples = np.array(samples).flatten()
    assert(np.allclose(samples, java_samples))


def test_consistency_cpu_parallel():
    """
    Verify that the random numbers generated by mrg_uniform, in parallel,
    are the same as the reference (Java) implementation by L'Ecuyer et al.

    """
    seed = 12345
    n_samples = 5
    n_streams = 12
    n_substreams = 7  # 7 samples will be drawn in parallel

    samples = []
    curr_rstate = np.array([seed] * 6, dtype='int32')

    for i in range(n_streams):
        stream_samples = []
        rstate = [curr_rstate.copy()]
        for j in range(1, n_substreams):
            rstate.append(rng_mrg.ff_2p72(rstate[-1]))
        rstate = np.asarray(rstate)
        rstate = theano.shared(rstate)

        new_rstate, sample = rng_mrg.mrg_uniform.new(rstate, ndim=None,
                                                     dtype=config.floatX,
                                                     size=(n_substreams,))
        # Not really necessary, just mimicking
        # rng_mrg.MRG_RandomStreams' behavior
        sample.rstate = rstate
        sample.update = (rstate, new_rstate)

        rstate.default_update = new_rstate
        f = theano.function([], sample)

        for k in range(n_samples):
            s = f()
            stream_samples.append(s)

        samples.append(np.array(stream_samples).T.flatten())

        # next stream
        curr_rstate = rng_mrg.ff_2p134(curr_rstate)

    samples = np.array(samples).flatten()
    assert(np.allclose(samples, java_samples))


def basictest(f, steps, sample_size, prefix="", allow_01=False, inputs=None,
              target_avg=0.5, target_std=None, mean_rtol=0.01, std_tol=0.01):
    if inputs is None:
        inputs = []
    dt = 0.0
    avg_var = 0.0

    for i in xrange(steps):
        t0 = time.time()
        ival = f(*inputs)
        assert ival.shape == sample_size
        dt += time.time() - t0
        ival = np.asarray(ival)
        if i == 0:
            mean = np.array(ival, copy=True)
            avg_var = np.mean((ival - target_avg) ** 2)
            min_ = ival.min()
            max_ = ival.max()
        else:
            alpha = 1.0 / (1 + i)
            mean = alpha * ival + (1 - alpha) * mean
            avg_var = (alpha * np.mean((ival - target_avg) ** 2) +
                       (1 - alpha) * avg_var)
            min_ = min(min_, ival.min())
            max_ = max(max_, ival.max())
        if not allow_01:
            assert min_ > 0
            assert max_ < 1

    if hasattr(target_avg, 'shape'):  # looks if target_avg is an array
        diff = np.mean(abs(mean - target_avg))
        # print prefix, 'mean diff with mean', diff
        assert np.all(diff < mean_rtol * (1 + abs(target_avg))), (
            'bad mean? %s %s' % (mean, target_avg))
    else:
        # if target_avg is a scalar, then we can do the mean of
        # `mean` to get something more precise
        mean = np.mean(mean)
        # print prefix, 'mean', mean
        assert abs(mean - target_avg) < mean_rtol * (1 + abs(target_avg)), (
            'bad mean? %f %f' % (mean, target_avg))

    std = np.sqrt(avg_var)
    # print prefix, 'var', avg_var
    # print prefix, 'std', std
    if target_std is not None:
        assert abs(std - target_std) < std_tol * (1 + abs(target_std)), (
            'bad std? %f %f %f' % (std, target_std, std_tol))
    # print prefix, 'time', dt
    # print prefix, 'elements', steps * sample_size[0] * sample_size[1]
    # print prefix, 'samples/sec', steps * sample_size[0] * sample_size[1] / dt
    # print prefix, 'min', min_, 'max', max_


def test_uniform():
    # TODO: test param low, high
    # TODO: test size=None
    # TODO: test ndim!=size.ndim
    # TODO: test bad seed
    # TODO: test size=Var, with shape that change from call to call
    if (config.mode in ['DEBUG_MODE', 'DebugMode', 'FAST_COMPILE'] or
            config.mode == 'Mode' and config.linker in ['py']):
        sample_size = (10, 100)
        steps = 50
    else:
        sample_size = (500, 50)
        steps = int(1e3)

    x = tensor.matrix()
    for size, const_size, var_input, input in [
            (sample_size, sample_size, [], []),
            (x.shape, sample_size, [x],
             [np.zeros(sample_size, dtype=config.floatX)]),
            ((x.shape[0], sample_size[1]), sample_size, [x],
             [np.zeros(sample_size, dtype=config.floatX)]),
            # test empty size (scalar)
            ((), (), [], []),
            ]:

        # TEST CPU IMPLEMENTATION
        # The python and C implementation are tested with DebugMode
        x = tensor.matrix()
        R = MRG_RandomStreams(234)
        # Note: we specify `nstreams` to avoid a warning.
        # TODO Look for all occurrences of `guess_n_streams` and `30 * 256`
        # for such situations: it would be better to instead filter the
        # warning using the warning module.
        u = R.uniform(size=size,
                      nstreams=rng_mrg.guess_n_streams(size, warn=False))
        f = theano.function(var_input, u)
        assert any([isinstance(node.op, theano.sandbox.rng_mrg.mrg_uniform)
                    for node in f.maker.fgraph.toposort()])
        f(*input)

        # Increase the number of steps if sizes implies only a few samples
        if np.prod(const_size) < 10:
            steps_ = steps * 100
        else:
            steps_ = steps
        basictest(f, steps_, const_size, prefix='mrg cpu', inputs=input)

        RR = theano.tensor.shared_randomstreams.RandomStreams(234)

        uu = RR.uniform(size=size)
        ff = theano.function(var_input, uu)
        # It's not our problem if numpy generates 0 or 1
        basictest(ff, steps_, const_size, prefix='numpy',
                  allow_01=True, inputs=input)


def test_broadcastable():
    R = MRG_RandomStreams(234)
    x = tensor.matrix()
    size1 = (10, 1)
    size2 = (x.shape[0], 1)
    pvals_1 = np.random.uniform(0, 1, size=size1)
    pvals_1 = pvals_1 / sum(pvals_1)
    pvals_2 = R.uniform(size=size2)
    pvals_2 = pvals_2 / tensor.sum(pvals_2)

    for distribution in [R.uniform, R.binomial, R.multinomial, R.multinomial_wo_replacement, R.normal]:
        # multinomial or multinomial_wo_replacement does not support "size" argument,
        # the sizes of them are implicitly defined with "pvals" argument.
        if distribution in [R.multinomial, R.multinomial_wo_replacement]:
            # check when all dimensions are constant
            uu = distribution(pvals=pvals_1)
            assert uu.broadcastable == (False, True)

            # check when some dimensions are theano variables
            uu = distribution(pvals=pvals_2)
            assert uu.broadcastable == (False, True)
        else:
            # check when all dimensions are constant
            uu = distribution(size=size1)
            assert uu.broadcastable == (False, True)

            # check when some dimensions are theano variables
            uu = distribution(size=size2)
            assert uu.broadcastable == (False, True)


@attr('slow')
def test_binomial():
    # TODO: test size=None, ndim=X
    # TODO: test size=X, ndim!=X.ndim
    # TODO: test random seed in legal value(!=0 and other)
    # TODO: test sample_size not a multiple of guessed #streams
    # TODO: test size=Var, with shape that change from call to call
    # we test size in a tuple of int and a tensor.shape.
    # we test the param p with int.

    if (config.mode in ['DEBUG_MODE', 'DebugMode', 'FAST_COMPILE'] or
            config.mode == 'Mode' and config.linker in ['py']):
        sample_size = (10, 50)
        steps = 50
        rtol = 0.02
    else:
        sample_size = (500, 50)
        steps = int(1e3)
        rtol = 0.01

    x = tensor.matrix()
    for mean in [0.1, 0.5]:
        for size, const_size, var_input, input in [
                (sample_size, sample_size, [], []),
                (x.shape, sample_size, [x],
                 [np.zeros(sample_size, dtype=config.floatX)]),
                # test empty size (scalar)
                ((), (), [], []),
                ]:
            yield (t_binomial, mean, size, const_size, var_input, input,
                   steps, rtol)


def t_binomial(mean, size, const_size, var_input, input, steps, rtol):
    R = MRG_RandomStreams(234)
    u = R.binomial(size=size, p=mean)
    f = theano.function(var_input, u)
    f(*input)

    # Increase the number of steps if sizes implies only a few samples
    if np.prod(const_size) < 10:
        steps_ = steps * 100
    else:
        steps_ = steps
    basictest(f, steps_, const_size, prefix='mrg  cpu',
              inputs=input, allow_01=True,
              target_avg=mean, mean_rtol=rtol)

    RR = theano.tensor.shared_randomstreams.RandomStreams(234)

    uu = RR.binomial(size=size, p=mean)
    ff = theano.function(var_input, uu)
    # It's not our problem if numpy generates 0 or 1
    basictest(ff, steps_, const_size, prefix='numpy', allow_01=True,
              inputs=input, target_avg=mean, mean_rtol=rtol)


@attr('slow')
def test_normal0():

    steps = 50
    std = 2.
    if (config.mode in ['DEBUG_MODE', 'DebugMode', 'FAST_COMPILE'] or
            config.mode == 'Mode' and config.linker in ['py']):
        sample_size = (25, 30)
        default_rtol = .02
    else:
        sample_size = (999, 50)
        default_rtol = .01
    sample_size_odd = (sample_size[0], sample_size[1] - 1)
    x = tensor.matrix()

    for size, const_size, var_input, input, avg, rtol, std_tol in [
        (sample_size, sample_size, [], [], -5., default_rtol, default_rtol),
        (x.shape, sample_size, [x],
         [np.zeros(sample_size, dtype=config.floatX)],
         -5., default_rtol, default_rtol),
        # test odd value
        (x.shape, sample_size_odd, [x],
         [np.zeros(sample_size_odd, dtype=config.floatX)],
         -5., default_rtol, default_rtol),
        (sample_size, sample_size, [], [],
         np.arange(np.prod(sample_size),
                   dtype='float32').reshape(sample_size),
         10. * std / np.sqrt(steps), default_rtol),
        # test empty size (scalar)
        ((), (), [], [], -5., default_rtol, 0.02),
        # test with few samples at the same time
        ((1,), (1,), [], [], -5., default_rtol, 0.02),
        ((3,), (3,), [], [], -5., default_rtol, 0.02),
            ]:

        R = MRG_RandomStreams(234)
        # Note: we specify `nstreams` to avoid a warning.
        n = R.normal(size=size, avg=avg, std=std,
                     nstreams=rng_mrg.guess_n_streams(size, warn=False))
        f = theano.function(var_input, n)
        f(*input)

        # Increase the number of steps if size implies only a few samples
        if np.prod(const_size) < 10:
            steps_ = steps * 50
        else:
            steps_ = steps
        basictest(f, steps_, const_size, target_avg=avg, target_std=std,
                  prefix='mrg ', allow_01=True, inputs=input,
                  mean_rtol=rtol, std_tol=std_tol)

        sys.stdout.flush()

        RR = theano.tensor.shared_randomstreams.RandomStreams(234)

        nn = RR.normal(size=size, avg=avg, std=std)
        ff = theano.function(var_input, nn)

        basictest(ff, steps_, const_size, target_avg=avg, target_std=std,
                  prefix='numpy ', allow_01=True, inputs=input, mean_rtol=rtol)


def basic_multinomialtest(f, steps, sample_size, target_pvals, n_samples,
                          prefix="", mean_rtol=0.04):

    dt = 0.0
    avg_pvals = np.zeros(target_pvals.shape, dtype=config.floatX)

    for i in xrange(steps):
        t0 = time.time()
        ival = f()
        assert ival.shape == sample_size
        assert np.all(np.sum(ival, axis=1) == n_samples)
        dt += time.time() - t0
        avg_pvals += ival
    avg_pvals /= (steps * n_samples)

    assert np.mean(abs(avg_pvals - target_pvals)) < mean_rtol

    print('random?[:10]\n', np.asarray(f()[:10]))
    print(prefix, 'mean', avg_pvals)
    # < mean_rtol, 'bad mean? %s %s' % (str(avg_pvals), str(target_pvals))
    print(np.mean(abs(avg_pvals - target_pvals)))
    print(prefix, 'time', dt)
    print(prefix, 'elements', steps * np.prod(target_pvals.shape))
    print(prefix, 'samples/sec', steps * np.prod(target_pvals.shape) / dt)


def test_multinomial():
    steps = 100

    if (config.mode in ['DEBUG_MODE', 'DebugMode', 'FAST_COMPILE'] or
            config.mode == 'Mode' and config.linker in ['py']):
        sample_size = (49, 5)
    else:
        sample_size = (450, 6)

    pvals = np.asarray(np.random.uniform(size=sample_size))
    pvals = np.apply_along_axis(lambda row: row / np.sum(row), 1, pvals)
    R = MRG_RandomStreams(234)
    # Note: we specify `nstreams` to avoid a warning.
    m = R.multinomial(pvals=pvals, dtype=config.floatX, nstreams=30 * 256)
    f = theano.function([], m)
    f()
    basic_multinomialtest(f, steps, sample_size, pvals, n_samples=1,
                          prefix='mrg ')


def test_multinomial_n_samples():
    if (config.mode in ['DEBUG_MODE', 'DebugMode', 'FAST_COMPILE'] or
            config.mode == 'Mode' and config.linker in ['py']):
        sample_size = (49, 5)
    else:
        sample_size = (450, 6)

    pvals = np.asarray(np.random.uniform(size=sample_size))
    pvals = np.apply_along_axis(lambda row: row / np.sum(row), 1, pvals)
    R = MRG_RandomStreams(234)

    for n_samples, steps in zip([5, 10, 100, 1000], [20, 10, 1, 1]):
        m = R.multinomial(pvals=pvals, n=n_samples,
                          dtype=config.floatX, nstreams=30 * 256)
        f = theano.function([], m)
        basic_multinomialtest(f, steps, sample_size, pvals,
                              n_samples, prefix='mrg ')
        sys.stdout.flush()


class T_MRG(unittest.TestCase):
    def test_bad_size(self):

        R = MRG_RandomStreams(234)

        for size in [
                (0, 100),
                (-1, 100),
                (1, 0),
                ]:

            self.assertRaises(ValueError, R.uniform, size)
            self.assertRaises(ValueError, R.binomial, size)
            self.assertRaises(ValueError, R.multinomial, size, 1, [])
            self.assertRaises(ValueError, R.normal, size)


def test_multiple_rng_aliasing():
    """
    Test that when we have multiple random number generators, we do not alias
    the state_updates member. `state_updates` can be useful when attempting to
    copy the (random) state between two similar theano graphs. The test is
    meant to detect a previous bug where state_updates was initialized as a
    class-attribute, instead of the __init__ function.

    """
    rng1 = MRG_RandomStreams(1234)
    rng2 = MRG_RandomStreams(2392)
    assert rng1.state_updates is not rng2.state_updates


def test_random_state_transfer():
    """
    Test that random state can be transferred from one theano graph to another.

    """
    class Graph:
        def __init__(self, seed=123):
            self.rng = MRG_RandomStreams(seed)
            self.y = self.rng.uniform(size=(1,))
    g1 = Graph(seed=123)
    f1 = theano.function([], g1.y)
    g2 = Graph(seed=987)
    f2 = theano.function([], g2.y)

    g2.rng.rstate = g1.rng.rstate
    for (su1, su2) in zip(g1.rng.state_updates, g2.rng.state_updates):
        su2[0].set_value(su1[0].get_value())

    np.testing.assert_array_almost_equal(f1(), f2(), decimal=6)


def test_gradient_scan():
    # Test for a crash when using MRG inside scan and taking the gradient
    # See https://groups.google.com/d/msg/theano-dev/UbcYyU5m-M8/UO9UgXqnQP0J
    theano_rng = MRG_RandomStreams(10)
    w = theano.shared(np.ones(1, dtype='float32'))

    def one_step(x):
        return x + theano_rng.uniform((1,), dtype='float32') * w

    x = tensor.vector(dtype='float32')
    values, updates = theano.scan(one_step, outputs_info=x, n_steps=10)
    gw = theano.grad(tensor.sum(values[-1]), w)
    f = theano.function([x], gw)
    f(np.arange(1, dtype='float32'))


def test_multMatVect():
    A1 = tensor.lmatrix('A1')
    s1 = tensor.ivector('s1')
    m1 = tensor.iscalar('m1')
    A2 = tensor.lmatrix('A2')
    s2 = tensor.ivector('s2')
    m2 = tensor.iscalar('m2')

    g0 = rng_mrg.DotModulo()(A1, s1, m1, A2, s2, m2)
    f0 = theano.function([A1, s1, m1, A2, s2, m2], g0)

    i32max = np.iinfo(np.int32).max

    A1 = np.random.randint(0, i32max, (3, 3)).astype('int64')
    s1 = np.random.randint(0, i32max, 3).astype('int32')
    m1 = np.asarray(np.random.randint(i32max), dtype="int32")
    A2 = np.random.randint(0, i32max, (3, 3)).astype('int64')
    s2 = np.random.randint(0, i32max, 3).astype('int32')
    m2 = np.asarray(np.random.randint(i32max), dtype="int32")

    f0.input_storage[0].storage[0] = A1
    f0.input_storage[1].storage[0] = s1
    f0.input_storage[2].storage[0] = m1
    f0.input_storage[3].storage[0] = A2
    f0.input_storage[4].storage[0] = s2
    f0.input_storage[5].storage[0] = m2

    r_a1 = rng_mrg.matVecModM(A1, s1, m1)
    r_a2 = rng_mrg.matVecModM(A2, s2, m2)
    f0.fn()
    r_b = f0.output_storage[0].value

    assert np.allclose(r_a1, r_b[:3])
    assert np.allclose(r_a2, r_b[3:])


def test_seed_fn():
    idx = tensor.ivector()

    for new_seed, same in [(234, True), (None, True), (23, False)]:
        random = MRG_RandomStreams(234)
        fn1 = theano.function([], random.uniform((2, 2), dtype='float32'))
        fn2 = theano.function([], random.uniform((3, 3), nstreams=2,
                                                 dtype='float32'))
        fn3 = theano.function([idx],
                              random.uniform(idx, nstreams=3, ndim=1,
                                             dtype='float32'))

        fn1_val0 = fn1()
        fn1_val1 = fn1()
        assert not np.allclose(fn1_val0, fn1_val1)
        fn2_val0 = fn2()
        fn2_val1 = fn2()
        assert not np.allclose(fn2_val0, fn2_val1)
        fn3_val0 = fn3([4])
        fn3_val1 = fn3([4])
        assert not np.allclose(fn3_val0, fn3_val1)
        assert fn1_val0.size == 4
        assert fn2_val0.size == 9

        random.seed(new_seed)

        fn1_val2 = fn1()
        fn1_val3 = fn1()
        fn2_val2 = fn2()
        fn2_val3 = fn2()
        fn3_val2 = fn3([4])
        fn3_val3 = fn3([4])
        assert np.allclose(fn1_val0, fn1_val2) == same
        assert np.allclose(fn1_val1, fn1_val3) == same
        assert np.allclose(fn2_val0, fn2_val2) == same
        assert np.allclose(fn2_val1, fn2_val3) == same
        assert np.allclose(fn3_val0, fn3_val2) == same
        assert np.allclose(fn3_val1, fn3_val3) == same


def rng_mrg_overflow(sizes, fct, mode, should_raise_error):
    for size in sizes:
        y = fct(size=size)
        f = theano.function([], y, mode=mode)
        if should_raise_error:
            assert_raises(ValueError, f)
        else:
            f()


def test_overflow_cpu():
    # run with THEANO_FLAGS=mode=FAST_RUN,device=cpu,floatX=float32
    rng = MRG_RandomStreams(np.random.randint(1234))
    fct = rng.uniform
    with change_flags(compute_test_value='off'):
        # should raise error as the size overflows
        sizes = [(2**31, ), (2**32, ), (2**15, 2**16,), (2, 2**15, 2**15)]
        rng_mrg_overflow(sizes, fct, config.mode, should_raise_error=True)
    # should not raise error
    sizes = [(2**5, ), (2**5, 2**5), (2**5, 2**5, 2**5)]
    rng_mrg_overflow(sizes, fct, config.mode, should_raise_error=False)
    # should support int32 sizes
    sizes = [(np.int32(2**10), ),
             (np.int32(2), np.int32(2**10), np.int32(2**10))]
    rng_mrg_overflow(sizes, fct, config.mode, should_raise_error=False)


def test_undefined_grad():
    srng = MRG_RandomStreams(seed=1234)

    # checking uniform distribution
    low = tensor.scalar()
    out = srng.uniform((), low=low)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out, low)

    high = tensor.scalar()
    out = srng.uniform((), low=0, high=high)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out, high)

    out = srng.uniform((), low=low, high=high)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out,
                  (low, high))

    # checking binomial distribution
    prob = tensor.scalar()
    out = srng.binomial((), p=prob)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out, prob)

    # checking multinomial distribution
    prob1 = tensor.scalar()
    prob2 = tensor.scalar()
    p = [theano.tensor.as_tensor_variable([prob1, 0.5, 0.25])]
    out = srng.multinomial(size=None, pvals=p, n=4)[0]
    assert_raises(theano.gradient.NullTypeGradError, theano.grad,
                  theano.tensor.sum(out), prob1)

    p = [theano.tensor.as_tensor_variable([prob1, prob2])]
    out = srng.multinomial(size=None, pvals=p, n=4)[0]
    assert_raises(theano.gradient.NullTypeGradError, theano.grad,
                  theano.tensor.sum(out), (prob1, prob2))

    # checking choice
    p = [theano.tensor.as_tensor_variable([prob1, prob2, 0.1, 0.2])]
    out = srng.choice(a=None, size=1, p=p, replace=False)[0]
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out[0],
                  (prob1, prob2))

    p = [theano.tensor.as_tensor_variable([prob1, prob2])]
    out = srng.choice(a=None, size=1, p=p, replace=False)[0]
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out[0],
                  (prob1, prob2))

    p = [theano.tensor.as_tensor_variable([prob1, 0.2, 0.3])]
    out = srng.choice(a=None, size=1, p=p, replace=False)[0]
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out[0],
                  prob1)

    # checking normal distribution
    avg = tensor.scalar()
    out = srng.normal((), avg=avg)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out, avg)

    std = tensor.scalar()
    out = srng.normal((), avg=0, std=std)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out, std)

    out = srng.normal((), avg=avg, std=std)
    assert_raises(theano.gradient.NullTypeGradError, theano.grad, out,
                  (avg, std))


def test_f16_nonzero(mode=None, op_to_check=rng_mrg.mrg_uniform):
    srng = MRG_RandomStreams(seed=utt.fetch_seed())
    m = srng.uniform(size=(1000, 1000), dtype='float16')
    assert m.dtype == 'float16', m.type
    f = theano.function([], m, mode=mode)
    assert any(isinstance(n.op, op_to_check) for n in f.maker.fgraph.apply_nodes)
    m_val = f()
    assert np.all((0 < m_val) & (m_val < 1))


if __name__ == "__main__":
    rng = MRG_RandomStreams(np.random.randint(2147462579))
    print(theano.__file__)
    pvals = theano.tensor.fmatrix()
    for i in range(10):
        t0 = time.time()
        multinomial = rng.multinomial(pvals=pvals)
        print(time.time() - t0)

from os import getpid
from pprint import pprint
from typing import NamedTuple, List
from uuid import UUID

from matplotlib.lines import Line2D
from matplotlib.legend_handler import HandlerTuple
from sklearn.linear_model import LinearRegression
from matplotlib.pyplot import close, subplots
from numpy import concatenate, zeros, array, array2string, ones, ndarray, average, empty, ceil, copy, Inf, isnan, \
    isinf, arange, where, log10
from numpy.random.mtrand import RandomState
import numpy as np
from pandas import DataFrame
from scipy.stats import pearsonr
from seaborn import axes_style, barplot, set_context, scatterplot, distplot

from pypuf.experiments.experiment.base import Experiment
from pypuf.learner.evolution_strategies.cmaes import ReliabilityBasedCMAES
from pypuf.simulation.arbiter_based.arbiter_puf import InterposePUF, XORArbiterPUF
from pypuf.simulation.arbiter_based.ltfarray import LTFArray
from pypuf.simulation.base import Simulation
from pypuf.studies.base import Study
from pypuf.tools import ChallengeResponseSet, TrainingSet, approx_dist, approx_dist_nonrandom, BIT_TYPE, random_inputs


class Parameters(NamedTuple):
    n: int
    k_up: int
    k_down: int
    N: int
    seed: int
    noisiness: float
    reps: int
    pop_size: int
    abort_delta: float
    abort_iter: int
    batch_size: int


class Result(NamedTuple):
    experiment_id: UUID
    measured_time: float
    pid: int
    max_memory: int
    accuracy: float
    accuracy_up: float
    accuracy_down: float
    accuracies: List[float]
    accuracies_up: List[float]
    accuracies_down: List[float]
    accuracies_down_flipped: List[float]
    correlation_down_front: object
    correlation_down_back: object
    correlation_up: object
    training_set_up_accuracy: List[float]
    training_set_down_accuracy: List[float]
    training_set_down_flipped_accuracy: List[float]
    training_set_up_sizes: List[int]
    rounds: int
    first_rounds: int
    simulation_noise: float
    iterations: int


class NoTrainingSetException(BaseException):
    pass


class SplitAttack(Experiment):
    simulation: InterposePUF
    simulation_noise_free: InterposePUF
    training_set: ChallengeResponseSet
    test_set: ChallengeResponseSet
    model_down: LTFArray
    model_up: LTFArray
    model: Simulation
    n2: int
    training_set_up_accuracy: List[float]
    training_set_down_accuracy: List[float]
    training_set_down_flipped_accuracy: List[float]
    training_set_up_sizes: List[int]
    accuracies: List[float]
    accuracies_up: List[float]
    accuracies_down: List[float]
    accuracies_down_flipped: List[float]
    iterations: int
    learner_up: ReliabilityBasedCMAES
    learner_down: ReliabilityBasedCMAES
    batch_size: int

    def __init__(self, progress_log_name, parameters):
        super().__init__(progress_log_name, parameters)
        assert self.parameters.n % 2 == 0, f'n must be multiple of 2, but was {self.parameters.n}'
        self.n2 = self.parameters.n // 2
        self.training_set_up_accuracy = []
        self.training_set_down_accuracy = []
        self.training_set_down_flipped_accuracy = []
        self.training_set_up_sizes = []
        self.accuracies = []
        self.accuracies_up = []
        self.accuracies_down = []
        self.accuracies_down_flipped = []
        self.rounds = 0
        self.first_rounds = 0
        self.iterations = 0
        self.learner_up = None
        self.max_rounds = 5 if max(self.parameters.k_down, self.parameters.k_up) < 5 else 1
        self.batch_size = self.parameters.batch_size

    def prepare(self):
        simulation_parameters = dict(
            n=self.parameters.n,
            k_down=self.parameters.k_down,
            k_up=self.parameters.k_up,
            transform='atf',
            seed=self.parameters.seed,
        )
        self.simulation = InterposePUF(
            **simulation_parameters,
            noisiness=self.parameters.noisiness,
            noise_seed=self.parameters.seed + 1,
        )
        self.simulation_noise_free = InterposePUF(
            **simulation_parameters,
        )
        self.progress_logger.debug('Split Attack starting ...')
        self.training_set = TrainingSet(
            instance=self.simulation,
            N=self.parameters.N,
            random_instance=RandomState(self.parameters.seed),
            reps=self.parameters.reps,
        )
        self.test_set = TrainingSet(
            instance=self.simulation,
            N=10 ** 4,
            random_instance=RandomState(self.parameters.seed + 1),
            reps=1,
        )
        self.progress_logger.debug(f'Training set size: {self.training_set.challenges.nbytes / 1024 ** 3:.2f}GiB')

    def run(self):
        self.progress_logger.debug('Creating initial training set down')
        training_set_down = self._interpose_crp_set_pm1(self.training_set)
        test_set_down = self._interpose_crp_set_pm1(self.test_set)
        self.progress_logger.debug('done')
        self.progress_logger.debug(f'Training set size: {training_set_down.challenges.nbytes / 1024 ** 3:.2f}GiB')
        self._att(training_set_down.challenges)  # transform training set in-situ to save memory
        self._att(test_set_down.challenges)

        while True:
            self.progress_logger.debug('computing first down model')
            self.model_down = self._get_first_model_down(xt_set=training_set_down)

            # attacker model accuracy
            model_ipuf = InterposePUF(
                n=self.parameters.n,
                k_down=self.parameters.k_down,
                k_up=self.parameters.k_up,
                transform='atf',
                seed=self.parameters.seed + 42,
            )
            model_ipuf.down = self.model_down
            test_set_accuracy = 1 - approx_dist_nonrandom(model_ipuf, self.test_set)

            # analysis: initial total accuracy
            self.accuracies.append(1 - approx_dist(model_ipuf, self.simulation, 10 ** 4, RandomState(1)))

            # analysis: down model accuracy
            self.progress_logger.debug('inital accuracy:')
            self._record_down_accuracy()

            # first model good?
            self.first_rounds += 1
            # if not (.45 <= test_set_accuracy <= .55) or self.first_rounds > 3:
            break

        del training_set_down
        del test_set_down

        # early stop?
        if .45 <= test_set_accuracy <= .55:
            self.progress_logger.debug('initial down model is bad, we give up')
            self.model_up = XORArbiterPUF(
                n=self.parameters.n,
                k=self.parameters.k_up,
                seed=1,
                transform='atf',
            )
            self.accuracies_up.append(-1)
            self._update_model()
            self.rounds = 0
            return

        """
        try:
            self.model_up = self._get_model_up()
        except NoTrainingSetException:
            self.progress_logger.debug('WARNING: could not create large enough training set for upper layer. '
                                       'Aborting!')
        """
        if not getattr(self, 'model_up', None):
            # use random model
            self.model_up = XORArbiterPUF(n=self.parameters.n, k=self.parameters.k_up,
                                          seed=self.parameters.seed + 27182)
            self.accuracies_up.append(-1)
        self.rounds += 1
        self._update_model()

    def _update_model(self):
        self.model = InterposePUF(
            n=self.parameters.n,
            k_down=self.parameters.k_down,
            k_up=self.parameters.k_up,
            transform='atf',
        )
        self.model.up = self.model_up
        self.model.down = self.model_down

        # analysis: model accuracy
        self.accuracies.append(1 - approx_dist(self.model, self.simulation, 10 ** 4, RandomState(1)))
        self.progress_logger.debug(f'current accuracy up: {self.accuracies_up[-1]:.2f}, '
                                   f'down: {self.accuracies_down[-1]:.2f}, '
                                   f'down flipped: {self.accuracies_down_flipped[-1]:.2f}, '
                                   f'total: {self.accuracies[-1]}')

    def _get_first_model_down(self, xt_set):
        self.progress_logger.debug('initially training down model')
        self.learner_down = ReliabilityBasedCMAES(
            training_set=xt_set,
            k=self.parameters.k_down,
            n=self.parameters.n + 1,
            transform=LTFArray.transform_id,  # note that we transformed the training set ourselves
            combiner='xor',
            # pop_size=self.parameters.pop_size,
            abort_delta=self.parameters.abort_delta,
            # abort_iter=self.parameters.abort_iter,
            random_seed=self.parameters.seed + 42,
            logger=self.progress_logger,
        )
        model, _ = self.learner_down.learn()
        self.learner_down.training_set = None
        self.learner_down.test_set = None
        self.iterations += self.learner_down.num_iterations
        model.transform = LTFArray.transform_atf  # note that we transformed the training set ourselves
        return model

    def _get_next_model_down(self):
        # create a training set for the lower PUF, based on the upper layer model
        self.progress_logger.debug(
            f'copying challenges of size {self.training_set.challenges.nbytes / 1024 ** 3:.2f}GiB')
        training_set = self._interpose_crp_set(self.training_set, self.model_up.eval(self.training_set.challenges))
        test_set = self._interpose_crp_set(self.test_set, self.model_up.eval(self.test_set.challenges))

        # analysis: training set accuracy
        self.training_set_down_accuracy.append(average(
            self.simulation.down.eval(training_set.challenges) == training_set.responses,
        ))
        self.training_set_down_flipped_accuracy.append(average(
            self._flip_model(self.simulation.down).eval(training_set.challenges) == training_set.responses,
        ))
        self.progress_logger.debug(f'new down training set accuracy: {self.training_set_down_accuracy[-1]:.2f}, '
                                   f'flipped: {self.training_set_down_flipped_accuracy[-1]:.2f}')

        # model training
        self.progress_logger.debug('re-training down model')
        self.learner_down.target_test_accuracy = None
        self.learner_down.test_set = test_set
        self.learner_down.training_set = training_set
        self._att(training_set.challenges)  # transform training set in-situ to save memory
        self._att(test_set.challenges)
        self.model_down, _ = self.learner_down.learn()
        self.learner_down.training_set = None
        self.learner_down.test_set = None
        self.model_down.transform = LTFArray.transform_atf  # note that we transformed the training set ourselves
        self._record_down_accuracy()
        self.iterations += self.learner_down.num_iterations

    def _get_model_up(self):
        # create a training set for the upper PUF, based on the lower layer model
        N, n = self.parameters.N, self.parameters.n
        challenges = self.training_set.challenges
        responses = self.training_set.responses

        self.progress_logger.debug('creating training set for upper layer')
        block_size = 10 ** 6

        # the max. number of challenges we select must be set up in advance to benefit from
        # numpy memory and cpu efficiency. While this number is non-deterministic and depends
        # on the given instance and training set, we expect it to be about N/2. To get a
        # reasonable upper bound, we add about 500MB of margin and round up to the next
        # multiple of block_size.
        N_selected = int((N / 2 + (500 * 1024 ** 2 / n)) // block_size + 1) * block_size
        selected_challenges = empty(shape=(N_selected, n), dtype=BIT_TYPE)
        self.progress_logger.debug(f'setting the max number of challenges for the upper training set to '
                                   f'{N_selected}, using {selected_challenges.nbytes / 1024 ** 3:.2f}GiB, about '
                                   f'{N_selected / N * 100:.2f}% of all challenges')
        selected_responses = empty(shape=(N_selected,), dtype=BIT_TYPE)
        filled = 0
        for idx in range(int(ceil(N / block_size))):
            # carve out a block to work on, max. size block_size, true size block_len
            block_slice = slice(idx * block_size, (idx + 1) * block_size)
            block_challenges = challenges[block_slice]
            block_responses = responses[block_slice]
            block_len = len(block_challenges)
            self.progress_logger.debug(f'working on block {idx} from {block_slice.start} to {block_slice.stop}, '
                                       f'total {block_len} challenges.')

            # create extended challenges (number: 2 * block_len)
            challenges_p1 = self._interpose(block_challenges, +ones(shape=(block_len, 1), dtype=BIT_TYPE))
            challenges_m1 = self._interpose(block_challenges, -ones(shape=(block_len, 1), dtype=BIT_TYPE))

            # evaluate extended challenges
            responses_p1 = self.model_down.eval(challenges_p1)
            responses_m1 = self.model_down.eval(challenges_m1)

            # identify challenges that depend on the interpose bit, i.e. yield unequal response
            # for unequal interpose bit
            responses_unequal = responses_p1 != responses_m1
            unequal = sum(responses_unequal)
            self.progress_logger.debug(f'found a total of {unequal} unequal responses out of {len(responses_p1)} '
                                       f'queries')

            # copy all these challenges (without interpose bit) to selected_challenges
            block_new = slice(
                filled,
                filled + unequal
            )
            selected_challenges[block_new] = block_challenges[responses_unequal]

            # to create the training set for the upper layer, we use the interpose bit that yielded
            # the response that matched our training set as the response of the upper layer, i.e.
            # select +1 if +1 as interpose bit yielded the correct response, or
            # select -1 if -1 as interpose bit yielded the correct response.
            # Let r be the response bit as recorded in the training set, rp1 the response bit
            # of the challenge with 1 as interpose bit. Then
            # selected_response = +1 if rp1 == r else -1.
            # This is the same as selected_response = rp1 * r.
            # We apply the product using numpy for all challenges at a time.
            selected_responses[block_new] = responses_p1[responses_unequal] * block_responses[responses_unequal]

            filled += unequal
            self.progress_logger.debug(f'wrote selected {{challenges, responses}} from {filled} to '
                                       f'{filled + unequal}')

        # cut off selected_challenges and selected_responses to the correct size
        self.training_set_up_sizes.append(filled)
        if filled < 50:
            raise NoTrainingSetException
        test_set_size = int(min(10 ** 4, max(.05 * filled, 1)))
        test_slice = slice(0, test_set_size)
        training_slice = slice(test_set_size, filled)
        test_set_up = ChallengeResponseSet(selected_challenges[test_slice], selected_responses[test_slice])
        training_set_up = ChallengeResponseSet(selected_challenges[training_slice], selected_responses[training_slice])
        self.progress_logger.debug(f'training and test set for upper layer created, sizes '
                                   f'{training_set_up.challenges.nbytes / 1024 ** 3:.2f}GiB, '
                                   f'{test_set_up.challenges.nbytes / 1024 ** 3:.2f}GiB')

        # analysis: training set accuracy
        self.training_set_up_accuracy.append(average(
            self.simulation.up.eval(training_set_up.challenges) == training_set_up.responses
        ))
        self.progress_logger.debug(f'new up training set accuracy: {self.training_set_up_accuracy[-1]:.2f} size: '
                                   f'{len(training_set_up.challenges)}')

        # train the upper model
        self.progress_logger.debug('(re)training up model')
        self._att(training_set_up.challenges)
        self._att(test_set_up.challenges)
        if not self.learner_up:
            ReliabilityBasedCMAES(
                training_set=training_set_up,
                n=self.parameters.n,
                k=self.parameters.k_up,
                transform=LTFArray.transform_id,
                combiner='xor',
                # pop_size=self.parameters.pop_size,
                abort_delta=self.parameters.abort_delta,
                # abort_iter=self.parameters.abort_iter,
                random_seed=self.parameters.seed + 43,
                logger=self.progress_logger,
            )
            model_up, _ = self.learner_up.learn()
        else:
            self.learner_up.training_set = training_set_up
            self.learner_up.test_set = test_set_up
            model_up, _ = self.learner_up.learn()
        self.learner_up.training_set = None
        self.learner_up.test_set = None
        model_up.transform = LTFArray.transform_atf
        self.accuracies_up.append(1 - approx_dist(model_up, self.simulation.up, 10 ** 4, RandomState(1)))
        self.progress_logger.debug(f'new up model accuracy: {self.accuracies_up[-1]:.2f}')
        self.iterations += self.learner_up.num_iterations

        return model_up

    @staticmethod
    def _weight_correlation(x, y):
        return array2string(
            array([[pearsonr(x[i, :], y[j, :])[0] for i in range(x.shape[0])] for j in range(y.shape[0])]),
            precision=1,
            floatmode='fixed',
            suppress_small=True,
        )

    def analyze(self):
        return Result(
            experiment_id=self.id,
            measured_time=self.measured_time,
            pid=getpid(),
            max_memory=self.max_memory(),
            accuracy=1 - approx_dist(self.simulation, self.model, 10 ** 4, RandomState(31415)),
            accuracy_up=1 - approx_dist(self.simulation.up, self.model_up, 10 ** 4, RandomState(31416)),
            accuracy_down=1 - approx_dist(self.simulation.down, self.model_down, 10 ** 4, RandomState(31417)),
            correlation_up=self._weight_correlation(
                self.simulation.up.weight_array,
                self.model_up.weight_array,
            ),
            correlation_down_front=self._weight_correlation(
                self.simulation.down.weight_array[:, :self.n2],
                self.model_down.weight_array[:, :self.n2],
            ),
            correlation_down_back=self._weight_correlation(
                self.simulation.down.weight_array[:, self.n2 + 1:],
                self.model_down.weight_array[:, self.n2 + 1:],
            ),
            training_set_up_accuracy=self.training_set_up_accuracy,
            training_set_down_accuracy=self.training_set_down_accuracy,
            training_set_down_flipped_accuracy=self.training_set_down_flipped_accuracy,
            training_set_up_sizes=self.training_set_up_sizes,
            accuracies=self.accuracies,
            accuracies_up=self.accuracies_up,
            accuracies_down=self.accuracies_down,
            accuracies_down_flipped=self.accuracies_down_flipped,
            rounds=self.rounds,
            first_rounds=self.first_rounds,
            simulation_noise=1 - approx_dist(self.simulation, self.simulation_noise_free, 10 ** 4, RandomState(31418)),
            iterations=self.iterations,
        )

    def _interpose(self, challenges, bits):
        if isinstance(bits, ndarray):
            N = challenges.shape[0]
            return concatenate((challenges[:, :self.n2], bits.reshape(N, 1), challenges[:, self.n2:]), axis=1)
        else:
            return concatenate(
                (
                    challenges[:, :self.n2],
                    zeros(shape=(challenges.shape[0], 1), dtype=BIT_TYPE) + bits,
                    challenges[:, self.n2:]
                ), axis=1
            )

    def _flip_model(self, model):
        flipped_weights = copy(model.weight_array)
        flipped_weights[:, self.n2 + 1:] = -flipped_weights[:, self.n2 + 1:]
        return LTFArray(
            weight_array=flipped_weights[:, :-1],
            transform=model.transform,
            combiner=model.combiner,
            bias=flipped_weights[:, -1],
        )

    def _record_down_accuracy(self):
        self.accuracies_down.append(1 - approx_dist(self.model_down, self.simulation.down, 10 ** 4, RandomState(1)))
        self.accuracies_down_flipped.append(
            1 - approx_dist(self._flip_model(self.model_down), self.simulation.down, 10 ** 4, RandomState(1)))
        self.progress_logger.debug(f'down model accuracy: {self.accuracies_down[-1]:.2f} / flipped: '
                                   f'{self.accuracies_down_flipped[-1]:.2f}')

    def _interpose_crp_set_pm1(self, crp_set: ChallengeResponseSet):
        return ChallengeResponseSet(
            challenges=self._interpose(
                challenges=crp_set.challenges,
                bits=random_inputs(1, crp_set.N, RandomState(self.parameters.seed)),
            ),
            responses=crp_set.responses,
        )

    def _interpose_crp_set(self, crp_set, interpose_bits):
        return ChallengeResponseSet(
            self._interpose(crp_set.challenges[:, :], interpose_bits),
            crp_set.responses
        )

    def _att(self, challenges):
        """
        Transformes the given challenges of shape (N, n) IN-SITU with the ATT transform.
        Also see LTFArray.att.
        """
        N, n = challenges.shape
        sub_challenges = challenges.reshape(N, 1, n)
        LTFArray.att(sub_challenges)


class SplitAttackStudy(Study):
    SHUFFLE = True

    @staticmethod
    def _noise_levels(n, k_up, k_down):
        if n != 64:
            return [0, 0.05, 0.1, 0.2]
        if k_down == 1:
            return [0, .1, .2]
        if k_up <= 4 and k_down <= 4:
            return [.1, .2, .5]
        return [0, .02, .05, .1]

    @staticmethod
    def _bit_lengths(k_up, k_down):
        if k_up <= 4 and k_down <= 4:
            return [32, 48, 56, 64, 72, 96, 128]
        return [64]

    @staticmethod
    def _batch_fracs():
        return [1.0]

    def experiments(self):
        M = 10 ** 6
        return [
            SplitAttack(
                progress_log_name=f'{self.name()}-n={n}-k_up={k_up}-k_down={k_down}-N={N}-noisiness={noisiness}-'
                                  f'seed={seed}',
                parameters=Parameters(
                    n=n,
                    k_up=k_up,
                    k_down=k_down,
                    N=int(N * n / 64),
                    seed=seed,
                    noisiness=noisiness,
                    reps=reps,
                    pop_size=pop_size,
                    abort_delta=abort_delta,
                    abort_iter=abort_iter,
                    batch_size=int(N * batch_frac),
                )
            )
            for k_up, k_down, Ns in [
                # (1, 1, [1000, 2000, 5000, 10000]),
                # (2, 2, [10000, 20000, 50000, 100000]),
                # (3, 3, [40000, 160000, 640000]),
                # (4, 4, [60000, 240000, 960000]),
                # (5, 5, [80000, 320000, 1280000]),
                # (6, 6, [1*M, 2*M, 5*M, 10*M, 15*M]),  # max. 7.5GB
                # (7, 7, [40*M]),  # 20GB VmPeak / 12GB VmRSS
                # (8, 8, [300*M]),  # est. 45 GB VmRSS peak
                # (9, 9, [700*M]),  # est. 90 GB VmRSS peak
                # (9, 9, 800000000),  # nearly 50 GB of training set size, needs ~75 GB
                # (1, 2, [100000]),
                # (1, 3, [10000, 20000, 50000, 100000]),
                (1, 3, [100000, 1 * M, 10 * M]),
                (3, 3, [100000, 1 * M, 10 * M]),
                (1, 4, [500000, 5 * M, 50 * M]),  # [10000, 20000, 50000, 100000]
                (4, 4, [500000, 5 * M, 50 * M]),
                # (1, 5, [200000, 500000, 600000, 1000000]),
                # (1, 6, [500000, 1000000, 2000000, 5000000]),
                # (1, 7, [20*M]),  # 2.5 GB peak memory usage
                # (1, 8, 150000000),
                # (1, 8, 200000000),  # ~17 GB
                # (1, 9, 350000000),  # ~32 GB
                # (1, 9, 450000000),  # ~42 GB
            ]
            for n in [64]
            for N in Ns
            # for n in self._bit_lengths(k_up, k_down)
            for noisiness in self._noise_levels(n, k_up, k_down)
            for reps in [23]
            for pop_size in [32]
            for abort_delta in [0.005]
            for abort_iter in [10]
            for seed in range(10)
            for batch_frac in self._batch_fracs()
        ]

    @classmethod
    def _Ncat(cls, N):
        return cls._cat(N, {
            'M': 1e6,
            'k': 1e3,
        })

    @classmethod
    def _time_cat(cls, time_s):
        return cls._cat(time_s, {
            'y': 365.25 * 24 * 60 ** 2,
            'w': 7 * 24 * 60 ** 2,
            'd': 24 * 60 ** 2,
            'h': 60 ** 2,
            'min': 60,
            's': 1,
        })

    @staticmethod
    def _cat(N, symb):
        if N == float('inf'):
            return '∞'
        for s, t in symb.items():
            if N >= t:
                r = N / t
                if int(r) == r:
                    return '%i%s' % (int(r), s)
                else:
                    return '%.2f%s' % (r, s)
        return '%i' % int(N)

    @staticmethod
    def _parse_array_of_float(a):
        if isinstance(a, float):
            return a
        a = a.lstrip('[ ')
        a = a.rstrip('] ')
        sep = ',' if ',' in a else ' '
        return [float(x.strip('[]')) for x in a.split(sep) if x.strip(' \n\t[]')]

    @staticmethod
    def _threads(row):
        if row['MKL_NUM_THREADS'] == row['NUMEXPR_NUM_THREADS'] == row['OMP_NUM_THREADS']:
            return row['MKL_NUM_THREADS']
        raise ValueError(f'Thread settings are awkward for row {row}.')

    def plot(self):
        data = self.experimenter.results.dropna(how='all')

        data['max_memory_gb'] = data.apply(lambda row: row['max_memory'] / 1024 ** 3, axis=1)
        data['Ne6'] = data.apply(lambda row: row['N'] / 1e6, axis=1)
        data['Ncat'] = data.apply(lambda row: self._Ncat(row['N']), axis=1)
        data['size'] = data.apply(lambda row: '(%i,%i)' % (int(row['k_up']), int(row['k_down'])), axis=1)
        data['measured_time'] = data.apply(lambda row: round(row['measured_time']), axis=1)
        data['success'] = data.apply(lambda row: row['accuracy'] >= .95 * row['simulation_noise'], axis=1)
        data['threads'] = data.apply(self._threads, axis=1)
        for field in ['accuracies_up', 'accuracies_down', 'accuracies', 'accuracies_down_flipped']:
            data[field] = data.apply(lambda row: self._parse_array_of_float(row[field]), axis=1)
            data[f'{field}_first'] = data.apply(
                lambda row: max(1 - row[field][0], row[field][0]) if not isinstance(row[field], float) else row[field],
                axis=1
            )
        data = data.sort_values(['size'])

        groups = data.groupby(['N', 'k_up', 'k_down', 'n', 'noisiness', 'threads', 'cpu'])
        rt_data = DataFrame(columns=['N', 'k_up', 'k_down', 'n', 'noisiness',
                                     'success_rate', 'avg_time_success', 'avg_time_fail', 'num_success', 'num_fail',
                                     'num_total', 'time_to_success', 'reliability', 'memory_avg', 'memory_max',
                                     'threads', 'cpu'])
        for (N, k_up, k_down, n, noisiness, threads, cpu), g_data in groups:
            num_success = len(g_data[g_data['success'] == True].index)
            num_total = len(g_data.index)
            success_rate = num_success / num_total
            mean_time_success = average(g_data[g_data['success'] == True]['measured_time'])
            mean_time_fail = average(g_data[g_data['success'] == False]['measured_time']) if success_rate < 1 else 0
            exp_number_of_trials_until_success = 1 / success_rate if success_rate > 0 else Inf  # Geometric dist.
            if isinf(exp_number_of_trials_until_success):
                time_to_success = Inf
            else:
                time_to_success = (exp_number_of_trials_until_success - 1) * mean_time_fail + mean_time_success
            reliability = g_data['simulation_noise'].mean()
            rt_data = rt_data.append(
                {
                    'N': N, 'k_up': k_up, 'k_down': k_down, 'n': n, 'noisiness': noisiness, 'threads': threads,
                    'cpu': cpu,
                    'success_rate': success_rate,
                    'avg_time_success': mean_time_success,
                    'avg_time_fail': mean_time_fail,
                    'num_success': num_success,
                    'num_fail': num_total - num_success,
                    'num_total': num_total,
                    'time_to_success': time_to_success,
                    'reliability': round(reliability * 100 // 10 * 10 / 100, 2),
                    'memory_avg_gib': g_data['max_memory'].mean() / 1024 ** 3,
                    'memory_max_gib': g_data['max_memory'].max() / 1024 ** 3,
                },
                ignore_index=True,
            )
        rt_data = rt_data.sort_values(['k_up', 'k_down', 'N', 'reliability'])

        rt_data['size'] = rt_data.apply(
            lambda row: '%i-bit\n(%i,%i)' % (int(row['n']), int(row['k_up']), int(row['k_down'])), axis=1)
        rt_data['Ncat'] = rt_data.apply(lambda row: self._Ncat(row['N']), axis=1)
        rt_data = rt_data[rt_data['reliability'] > .6]

        set_context('paper')
        with axes_style('whitegrid'):
            """
            Plot 1: Expected time to success, comparing for different iPUF sizes, training set sizes, and levels of
            reliability. All 64 bit.
            """
            hues = ['reliability', 'Ncat']
            f, axes = subplots(ncols=1, nrows=2 * len(hues))
            data_64 = rt_data[rt_data['n'] == 64]
            for idx, hue in enumerate(hues):
                data_64['x'] = data_64.apply(
                    lambda row: '\n'.join(['%s' % row[h] for h in hues + ['size'] if h != hue]),
                    axis=1)
                self._barplot(data_64[data_64['k_up'] == 1], axes[idx], hue, hues)
                self._barplot(data_64[data_64['k_up'] != 1], axes[idx + len(hues)], hue, hues)
            f.set_size_inches(15, 3 * 2 * len(hues))
            f.subplots_adjust(hspace=.45)
            f.savefig(f'figures/{self.name()}.pdf', bbox_inches='tight', )
            close(f)

            """
            Plot 2: Comparing expected time to success for different bit lengths and iPUF sizes (k,k) with k<=4. No
            noise.
            """
            opt_data = DataFrame(columns=['k_up', 'k_down', 'n', 'N_best', 'N_best_cat', 'reliability',
                                          'memory_avg_gib', 'success_rate', 'num_total', 'threads', 'cpu'])
            for (k_up, k_down, n, reliability, threads, cpu), group in rt_data.groupby(
                    ['k_up', 'k_down', 'n', 'reliability', 'threads', 'cpu']
            ):
                opt_data = opt_data.append(
                    {
                        'k_up': k_up, 'k_down': k_down, 'n': n, 'reliability': reliability, 'threads': threads,
                        'cpu': cpu,
                        'success_rate': group.loc[group['time_to_success'].idxmin()]['success_rate'],
                        'num_total': group.loc[group['time_to_success'].idxmin()]['num_total'],
                        'time_to_success_best': group['time_to_success'].min(),
                        'N_best': group.loc[group['time_to_success'].idxmin()]['N'],
                        'N_best_cat': self._Ncat(group.loc[group['time_to_success'].idxmin()]['N']),
                        'memory_avg_gib': group['memory_avg_gib'].mean(),
                    },
                    ignore_index=True,
                )
            opt_data = opt_data.sort_values(['k_up', 'k_down', 'n', 'N_best', 'reliability'])
            opt_data['iPUF Type'] = opt_data.apply(lambda row: '(%.0f, %.0f)' % (row['k_up'], row['k_down']), axis=1)
            opt_data['S'] = opt_data.apply(lambda row: '(%.0f, %.0f)' % (row['k_up'], row['k_down']), axis=1)

            n_set = list(map(int, opt_data['n'].unique()))
            stable_opt_data = opt_data[opt_data['reliability'] == 1]
            stable_opt_data = stable_opt_data[stable_opt_data['cpu'] != "Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz"]
            # n_data_64 = n_data_64[(n_data_64['S'] != "(8, 8)") |\
            # (n_data_64['cpu'] != "Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz")]
            nonempty_sizes = [l for (l, f) in stable_opt_data.groupby(['iPUF Type']) if f['n'].nunique() > 1]

            ax = scatterplot(
                data=stable_opt_data[stable_opt_data['iPUF Type'].isin(nonempty_sizes)],
                x='n',
                y='time_to_success_best',
                style='iPUF Type',
                markers=True,
                ci=None,
                legend=None,
            )

            lr_range = list(range(32, 257))
            formulas = []
            linestyles = ['solid', 'dotted', 'dashed', 'dashdot']
            colors = ['C1', 'C2', 'C3', 'C4']
            markers = ['.', 'x', 's', '+']
            for k, ls, c, m in zip(range(1, 5), linestyles, colors, markers):
                lr = LinearRegression()
                lr_data = stable_opt_data[stable_opt_data['iPUF Type'].isin(nonempty_sizes)]
                lr_data = lr_data[lr_data['k_down'] == k]
                lr.fit(array(log10([lr_data['n']])).T, array([log10(lr_data['time_to_success_best'])]).T)
                appr_text = "$ \\approx %f \cdot n^{%f}$" % (10 ** lr.intercept_[0], lr.coef_[0][0])
                p = ax.plot(
                    lr_range,
                    10 ** lr.predict(array([log10(array(lr_range))]).T)[:, 0],
                    alpha=0.7,
                    linestyle=ls,
                    color=c,
                )
                print("%f mal n hoch %f" % (10 ** lr.intercept_[0], lr.coef_[0][0]))
                formulas.append(appr_text)

            leg_keys = []
            leg_texts = []
            for k, ls, c, txt, m in zip(range(1, 5), linestyles, colors, formulas, markers):
                custom_line = Line2D([0], [0], color=c, lw=1.2, ls=ls)
                custom_symb = Line2D([], [], color='C0', marker=m, linestyle='None')
                leg_keys = [(custom_symb, custom_line)] + leg_keys
                leg_texts = ['({},{})  {}'.format(k, k, txt)] + leg_texts
            leg = ax.legend(leg_keys, leg_texts, handler_map={tuple: HandlerTuple(ndivide=None)}, handlelength=4,
                            loc='best')

            print(stable_opt_data)
            # ax.legend(loc=0, borderaxespad=0.)

            f = ax.get_figure()
            ticks = {'1min': 60, '5min': 5 * 60,
                     '20min': 20 * 60, '1h': 60 * 60,
                     '4h': 4 * 60 ** 2, '1d': 24 * 60 ** 2, '1w': 7 * 24 * 60 ** 2, }
            ax.set_xscale('log')
            ax.set_yscale('log')
            ax.set_yticks(list(ticks.values()))
            ax.set_yticklabels(list(ticks.keys()))
            ax.set_ylabel('Time Until First Success')
            ax.set_xticks([n for n in n_set])
            ax.set_xticklabels([str(n) for n in n_set])
            ax.set_xticklabels([], minor=True)
            f.set_size_inches(5.5 * 1.2, 2.0 * 1.2)
            f.suptitle('Attack Time for Noise-Free Interpose PUFs by Challenge Length n')

            plottrainsetsize = False
            if plottrainsetsize:
                # Trainsetsize
                ax2 = ax.twinx()
                ax2.set(yscale="log")
                ax2.grid(False)

                X = stable_opt_data
                t = ax2.bar(x=X['n'],
                            height=X['N_best'],
                            # width=0.2,
                            align='edge',
                            alpha=0.35,
                            linestyle='--',
                            edgecolor='black',
                            linewidth=1,
                            facecolor=None,
                            )

            ax.tick_params(axis='y', which='minor', left=False)
            f.savefig(f'figures/{self.name()}.n.pdf', bbox_inches='tight', )
            close(f)

            """
            Plot 3: Comparing expected time to success for different (1,k) and (k,k) sizes, full reliability assumed.
            """
            n_data_64 = opt_data[(opt_data['n'] == 64) &
                                 ((opt_data['reliability'] == .8) | (opt_data['reliability'] == 1)) &
                                 ((opt_data['k_up'] >= 3) | (opt_data['k_down'] >= 3))].sort_values(
                ['n', 'k_down', 'reliability'])
            n_data_64['iPUF Type'] = n_data_64.apply(lambda row: '(1,k)' if row['k_up'] == 1 else '(k,k)', axis=1)
            n_data_64['k'] = n_data_64['k_down']
            # Filtering
            n_data_64 = n_data_64[n_data_64['num_total'] >= 10]
            n_data_64 = n_data_64[n_data_64['cpu'] != "Intel(R) Xeon(R) CPU E7- 4870  @ 2.40GHz"]
            n_data_64 = n_data_64[
                (n_data_64['S'] != "(8, 8)") | (n_data_64['cpu'] != "Intel(R) Xeon(R) Gold 6130 CPU @ 2.10GHz")]

            k_max = int(max(n_data_64['k_up'].max(), n_data_64['k_down'].max()))
            reliabilities = sorted(n_data_64['reliability'].unique())

            ticks = {'5min': 5 * 60,
                     '20min': 20 * 60, '2h': 2 * 60 ** 2,
                     '8h': 8 * 60 ** 2, '2d': 2 * 24 * 60 ** 2, '1w': 7 * 24 * 60 ** 2, }

            tsticks = {'20k': 20 * 10 ** 3, '50k': 50 * 10 ** 3, '200k': 2 * 10 ** 5, '500k': 5 * 10 ** 5,
                       '2M': 2 * 10 ** 6, '5M': 5 * 10 ** 6, '20M': 20 * 10 ** 6,
                       '50M': 50 * 10 ** 6, '200M': 200 * 10 ** 6, }

            fig, axs = subplots(ncols=2, nrows=2, sharey='row', sharex='col', figsize=(6.4, 4.0))

            tmp_data = n_data_64[n_data_64['reliability'] == 0.8]
            print(tmp_data)
            ax = scatterplot(data=tmp_data,
                             x="k",
                             y="time_to_success_best",
                             markers=True,
                             style="iPUF Type",
                             ax=axs[0][0],
                             )
            ax.set_yscale('log')
            ax.set_xlabel('k')
            ax.set_ylabel('Time until first success')
            ax.set_yticks(list(ticks.values()), minor=False)
            ax.set_yticklabels(list(ticks.keys()))
            ax.title.set_text('Reliability 0.8')

            custom_lines = [Line2D([0], [0], color='C0', lw=1.2, ls='-'),
                            Line2D([0], [0], color='C0', lw=1.2, ls=(0.0, [2.5, 0.75]))]
            custom_symbs = [Line2D([], [], color='C0', marker='.', linestyle='None'),
                            Line2D([], [], color='C0', marker='x', linestyle='None')]
            leg_keys = zip(custom_symbs, custom_lines)
            leg = ax.legend(leg_keys, ['(1,k)', '(k,k)'], handler_map={tuple: HandlerTuple(ndivide=None)})

            lr_range = list(range(3, 9))
            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(1,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['time_to_success_best'])]).T)
            p1 = ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                alpha=0.7,
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.4f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(3, 21 * 60, appr_text, color='C0', alpha=0.9, usetex=True)

            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(k,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['time_to_success_best'])]).T)
            p2 = ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                '--',
                alpha=0.7,
                color='C0'
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.4f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(6, 21 * 60, appr_text, color='C0', alpha=0.9, usetex=True)

            ax = scatterplot(data=tmp_data,
                             x="k",
                             y="N_best",
                             markers=True,
                             style="iPUF Type",
                             legend=None,
                             ax=axs[1][0]
                             )
            ax.set_yscale('log')
            ax.set_xlabel('k')
            ax.set_ylabel('Training set size')
            ax.title.set_text('Reliability 0.8')
            ax.set_yticks(list(tsticks.values()), minor=False)
            ax.set_yticklabels(list(tsticks.keys()))

            lr_range = list(range(3, 9))
            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(1,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['N_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                alpha=0.7,
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.2f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(3, 2 * 10 ** 6, appr_text, color='C0', alpha=0.9, usetex=True)

            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(k,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['N_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                '--',
                alpha=0.7,
                color='C0'
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.2f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(6, 500000, appr_text, color='C0', alpha=0.9, usetex=True)

            tmp_data = n_data_64[n_data_64['reliability'] == 1.0]
            print(tmp_data)
            ax = scatterplot(data=tmp_data,
                             x="k",
                             y="time_to_success_best",
                             markers=True,
                             style="iPUF Type",
                             legend=None,
                             ax=axs[0][1],
                             )
            ax.set_yscale('log')
            ax.set_xlabel('k')
            ax.set_ylabel('Time until first success')
            ax.set_yticks(list(ticks.values()), minor=False)
            ax.set_yticklabels(list(ticks.keys()))
            ax.title.set_text('Reliability 1.0')

            lr_range = list(range(3, 9))
            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(1,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['time_to_success_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                alpha=0.7,
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.4f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(3, 21 * 60, appr_text, color='C0', alpha=0.9, usetex=True)

            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(k,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['time_to_success_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                '--',
                alpha=0.7,
                color='C0'
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.4f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(6, 21 * 60, appr_text, color='C0', alpha=0.9, usetex=True)

            ax = scatterplot(data=tmp_data,
                             x="k",
                             y="N_best",
                             markers=True,
                             style="iPUF Type",
                             legend=None,
                             ax=axs[1][1]
                             )
            ax.set_yscale('log')
            ax.set_xlabel('k')
            ax.set_ylabel('Training set size')
            ax.title.set_text('Reliability 1.0')
            ax.set_yticks(list(tsticks.values()), minor=False)
            ax.set_yticklabels(list(tsticks.keys()))

            lr_range = list(range(3, 9))
            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(1,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['N_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                alpha=0.7,
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.2f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(3, 2 * 10 ** 6, appr_text, color='C0', alpha=0.9, usetex=True)

            lr = LinearRegression()
            lr_data = tmp_data[tmp_data['iPUF Type'] == "(k,k)"]
            lr.fit(array([lr_data['k']]).T, array([log10(lr_data['N_best'])]).T)
            ax.plot(
                lr_range,
                10 ** lr.predict(np.array([lr_range]).T)[:, 0],
                '--',
                alpha=0.7,
                color='C0'
            )
            print("%f mal %f hoch x" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0]))
            appr_text = "$%.2f \cdot %.3f^k$" % (10 ** lr.intercept_[0], 10 ** lr.coef_[0][0])
            ax.text(6, 500000, appr_text, color='C0', alpha=0.9, usetex=True)

            """
            from matplotlib.lines import Line2D
            custom_lines = [Line2D([0], [0], color='black', lw=1.2, ls='-'),
                            Line2D([0], [0], color='black', lw=1.2, ls='--')]
            g = relplot(
                data=n_data_64,
                x='k',
                y='time_to_success_best',
                row='reliability',
                kind='line',
                markers=True,
                style='iPUF Type',
                ci=None,
                aspect=3,
                height=2.0,
                facet_kws={"legend_out":False}
            )
            g.axes.flat[0].legend(custom_lines[:2], ['(1,k)', '(k,k)'], title='iPUF Type')
            for idx, ax in enumerate(g.axes.flat):
                ax.set_xscale('log')
                ax.set_yscale('log')
                ax.set_yticks(list(ticks.values()), minor=False)
                ax.set_yticklabels(list(ticks.keys()))
                ax.set_xticks([k for k in range(1, k_max + 1)])
                ax.set_xticklabels([str(k) for k in range(1, k_max + 1)])
                ax.set_xticklabels([], minor=True)
                ax2 = ax.twinx()
                ax2.set(yscale="log")
                ax2.grid(False)
                D = n_data_64[n_data_64['reliability'] == reliabilities[idx]]
                print(D)
                # Remove duplicates with worst time
                D = D.sort_values('time_to_success_best', ascending=True).drop_duplicates(['k_up','k_down']).sort_index()
                print(D)
                X = D[D["iPUF Type"] == '(k,k)']
                t = ax2.bar(x=X['k'],
                    height=X['N_best'],
                    width=-0.2,
                    align='edge',
                    alpha=0.35,
                    linestyle='--',
                    edgecolor='black',
                    linewidth=1,
                    facecolor=None,
                    )
                for p in t.patches:
                    p.set_x(p.get_x() - 0.05)
                X = D[D["iPUF Type"] == '(1,k)']
                t = ax2.bar(x=X['k'],
                    height=X['N_best'],
                    width=0.2,
                    align='edge',
                    alpha=0.35,
                    linestyle='-',
                    edgecolor='black',
                    linewidth=1,
                    facecolor=None,
                    )
                for p in t.patches:
                    p.set_x(p.get_x() + 0.05)
                ax.tick_params(axis='y', which='minor', left=False)
                if idx == len(g.axes.flat) // 2:
                    ax.set_ylabel('Time Until First Success')
                    ax2.set_ylabel('#CRP')
                else:
                    ax.set_ylabel('')
                    ax2.set_ylabel('')
            """

            fig.savefig(f'figures/{self.name()}.size.pdf', bbox_inches='tight', )

            """
            Plot 4: Accuracy distribution for initial training of the lower layer
            """
            data['initial_down_accuracy'] = data.apply(
                lambda row: max([row[a] for a in ['accuracies_down_first', 'accuracies_down_flipped_first']]),
                axis=1,
            )
            bin_width = .01
            fig_data = opt_data[(opt_data['n'] == 64) & (opt_data['reliability'] == 1) & (opt_data['k_down'] >= 4) &
                                (opt_data['num_total'] >= 10)]
            fig_data['cat'] = fig_data.apply(lambda row: '1k' if row['k_up'] == 1 else 'kk', axis=1)
            fig, axes = subplots(nrows=1, ncols=2, sharex=True)
            ax_1k = axes[0]
            ax_kk = axes[1]
            for cat, group in fig_data.groupby(['cat']):
                ax = ax_1k if cat == '1k' else ax_kk
                for _, row in group.iterrows():
                    distplot(
                        data[(data['n'] == 64) & (data['simulation_noise'] == 1) &
                             (data['k_up'] == row['k_up']) & (data['k_down'] == row['k_down']) &
                             (data['N'] == row['N_best'])]['initial_down_accuracy'],
                        bins=arange(.5, 1 + bin_width, bin_width),
                        norm_hist=True,
                        kde=None,
                        ax=ax,
                    )
                ax.legend(
                    [
                        f'({row["k_up"]:.0f}, {row["k_down"]:.0f}) with {row["N_best_cat"]} CRPs'
                        for _, row in group.iterrows()
                    ],
                    loc='upper left',
                    bbox_to_anchor=(.1, 1),
                )
                ax.set_xlim((.49, 1))
                ax.set_xticks(arange(.5, 1.01, .1))
                ax.set_yticks([])
                ax.set_xlabel('')
                ax.set_ylabel('Rel. Frequency')
            fig.suptitle('Accuracy of Lower Layer Model after Linearization Attack')
            scale = 2.5
            fig.set_size_inches(w=3 * scale, h=.8 * scale)
            fig.tight_layout(rect=[0, 0.03, 1, 0.95])
            fig.savefig(f'figures/{self.name()}.initial.pdf')

            """
            Table 1: Details on Optimal Attack Settings for n=64
            """
            unique_cpus = opt_data['cpu'].unique()
            numerals = ['one', 'two', 'three', 'four', 'five']
            opt_data['cpu_num'] = opt_data.apply(lambda row: where(unique_cpus == row['cpu'])[0][0], axis=1)
            opt_data['time_to_success_best_cat'] = opt_data.apply(
                lambda row: self._time_cat(row['time_to_success_best']) +
                            f' ({row["threads"]:.0f}/\\cpu{numerals[row["cpu_num"]]})',
                axis=1
            )
            table_1 = opt_data[opt_data['n'] == 64][['S', 'N_best_cat', 'reliability', 'memory_avg_gib',
                                                     'time_to_success_best_cat', 'success_rate',
                                                     'num_total']]
            table_1['memory_avg_gib'] = table_1['memory_avg_gib'].round(1)
            table_1['success_rate'] = table_1['success_rate'].round(2)
            table_1 = table_1[table_1['num_total'] >= 10]
            print(table_1.to_latex(index=False, escape=False))

            pprint('Used CPUs:')
            pprint({numerals[i]: cpu for i, cpu in enumerate(unique_cpus)})

            data['cpu_time'] = data.apply(lambda row: row['threads'] * row['measured_time'], axis=1)
            print(f'This study includes a total of {self._time_cat(data["cpu_time"].sum())} CPU time.')

    def _barplot(self, data, ax, hue, hues):
        barplot(
            data=data[~isnan(data['time_to_success'])],
            x='x',
            y='time_to_success',
            hue=hue,
            ci=None,
            ax=ax,
        )
        ticks = {'30s': 30, '5min': 5 * 60, '20min': 20 * 60, '1h': 3600}
        ticks.update({'6h': 6 * 3600, '1d': 24 * 3600})
        ax.set_yscale('log')
        ax.set_yticks(list(ticks.values()))
        ax.set_yticklabels(list(ticks.keys()))
        ax.set_xlabel(' / '.join([h for h in hues + ['size'] if h != hue]))
        ax.set_ylabel('Attack Time Until First Success')
        ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left', borderaxespad=0.).set_title(hue)

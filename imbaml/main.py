import logging
from typing import Union, Callable

import numpy as np
import pandas as pd
import ray
from hyperopt import hp, STATUS_OK
from imbens.ensemble import AdaCostClassifier
from ray.tune import ResultGrid
from ray.tune.search import ConcurrencyLimiter
from ray.tune.search.hyperopt import HyperOptSearch
from sklearn.metrics import make_scorer, f1_score, balanced_accuracy_score, average_precision_score, recall_score, \
    precision_score
from sklearn.model_selection import cross_val_score, StratifiedKFold

from imbaml.search_spaces.classical.ensemble.bag import ExtraTreesGenerator
from imbaml.search_spaces.classical.ensemble.boost import XGBClassifierGenerator, LGBMClassifierGenerator
from imbaml.search_spaces.balanced.ensemble.bag import BalancedRandomForestClassifierGenerator, BalancedBaggingClassifierGenerator
from imbaml.search_spaces.balanced.ensemble.boost import AdaReweightedGenerator
from imbaml.search_spaces.classical.mlp import MLPClassifierGenerator

logger = logging.getLogger(__name__)


#TODO: use ray.tune.Trainable directly
#
# class RayTrainable(ray.tune.Trainable):
#     def setup(self, config):
#         self.algorithm_configuration = config["algorithm_configuration"]
#         self.metric = config["metric"]
#         self.X = config['X']
#         self.y = config['y']
#
#     def step(self):
#         trial_result = ImbaExperimentRunner.compute_metric_score(
#             self.algorithm_configuration,
#             self.metric,
#             self.X,
#             self.y)
#         return {"loss": trial_result['loss']}
class RayTuner:
    @staticmethod
    def trainable(config):
        trial_result = Imba.compute_metric_score(
            config['search_configurations'],
            config['metric'],
            config['X'],
            config['y'])
        ray.train.report(trial_result)


class Imba:
    """
    Imba is a class for optimizing machine learning models using hyperparameter tuning.

    Attributes:
        _metric (str): The performance metric to optimize (e.g., 'f1', 'balanced_accuracy').
        _n_evals (int): The number of evaluations to perform during hyperparameter tuning.
    
    Methods:
        __init__(metric, n_evals=60, re_init=True):
            Initializes the Imba class with a specified metric and number of evaluations.
        
        _re_init():
            Reinitializes the Ray framework with specified configurations.
        
        compute_metric_score(hyper_parameters, metric, X, y):
            Computes the metric score for a given set of hyperparameters using cross-validation.
        
        fit(X, y):
            Fits the model to the provided data and performs hyperparameter tuning based on the specified metric.
    """
    def __init__(self, metric, n_evals=60, re_init=True):
        self._metric = metric
        self._n_evals = n_evals
        if re_init:
            Imba._re_init()

    @staticmethod
    def _re_init():
        ray.init(object_store_memory=10**9, log_to_driver=False, logging_level=logging.ERROR)

    @classmethod
    def compute_metric_score(cls, hyper_parameters, metric, X, y):
        hyper_parameters = hyper_parameters.copy()
        model_class = hyper_parameters.pop('model_class')
        clf = model_class(**hyper_parameters)

        loss_value = cross_val_score(
            estimator=clf,
            X=X,
            y=y,
            cv=StratifiedKFold(n_splits=8),
            scoring=make_scorer(metric),
            error_score='raise').mean()

        return {'loss': -loss_value, 'status': STATUS_OK}

    def fit(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series]
    ) -> ResultGrid:
        metric: Callable
        if self._metric == 'f1':
            metric = f1_score
        elif self._metric == 'balanced_accuracy':
            metric = balanced_accuracy_score
        elif self._metric == 'average_precision':
            metric = average_precision_score
        elif self._metric == 'recall':
            metric = recall_score
        elif self._metric == 'precision':
            metric = precision_score
        else:
            raise ValueError(f"Metric {self._metric} is not supported.")

        dataset_size_in_mb = int(pd.DataFrame(X).memory_usage(deep=True).sum() / (1024 ** 2))
        logger.info(f"Dataset size: {dataset_size_in_mb} mb.")

        n_evals = self._n_evals
        if dataset_size_in_mb > 50:
            n_evals //= 4
        elif dataset_size_in_mb > 5:
            n_evals //= 3

        logger.info(f"Number of optimization search trials: {n_evals}.")

        search_space = [
            XGBClassifierGenerator.generate(),
            AdaReweightedGenerator.generate(AdaCostClassifier),
            BalancedRandomForestClassifierGenerator.generate(),
            BalancedBaggingClassifierGenerator.generate(),
            LGBMClassifierGenerator.generate(),
            ExtraTreesGenerator.generate(),
            MLPClassifierGenerator.generate()
        ]

        search_configurations = hp.choice("search_configurations", search_space)

        ray_configuration = {
            'X': X,
            'y': y,
            'metric': metric,
            'search_configurations': search_configurations
        }

        # HyperOptSearch(points_to_evaluate = promising initial points)
        search_algo = ConcurrencyLimiter(
            HyperOptSearch(
                space=ray_configuration,
                metric='loss',
                mode='min'),
            max_concurrent=5,
            batch=True)

        tuner = ray.tune.Tuner(
            RayTuner.trainable,
            tune_config=ray.tune.TuneConfig(
                metric='loss',
                mode='min',
                search_alg=search_algo,
                num_samples=n_evals),
            # run_config=ray.train.RunConfig(
            #     stop={"training_iteration": 1},
            #     checkpoint_config=ray.train.CheckpointConfig(
            #         checkpoint_at_end=False
            #     )
            # )
            # reuse_actors=True),
            # param_space=ray_configuration
        )

        return tuner.fit()

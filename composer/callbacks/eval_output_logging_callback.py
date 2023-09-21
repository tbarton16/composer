# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""Log model outputs and expected outputs during ICL evaluation."""

import hashlib
import os
import random
import shutil
import time
from typing import Callable, Optional

import pandas as pd
from composer.loggers.console_logger import ConsoleLogger
from torch.utils.data import DataLoader

from composer.core import Callback, State
from composer.datasets.in_context_learning_evaluation import (InContextLearningCodeEvalDataset,
                                                              InContextLearningLMTaskDataset,
                                                              InContextLearningMultipleChoiceTaskDataset,
                                                              InContextLearningQATaskDataset,
                                                              InContextLearningSchemaTaskDataset)
from composer.loggers import Logger
from composer.utils import maybe_create_object_store_from_uri, parse_uri

ICLDatasetTypes = (InContextLearningLMTaskDataset, InContextLearningQATaskDataset,
                   InContextLearningMultipleChoiceTaskDataset, InContextLearningSchemaTaskDataset,
                   InContextLearningCodeEvalDataset)


def _write(destination_path, src_file):
    obj_store = maybe_create_object_store_from_uri(destination_path)
    _, _, save_path = parse_uri(destination_path)
    if obj_store is not None:
        obj_store.upload_object(object_name=save_path, filename=src_file)
    else:
        shutil.copy(src_file, destination_path)


class EvalOutputLogging(Callback):
    """Logs eval outputs for each sample of each ICL evaluation dataset.

    ICL metrics are required to support caching the model's responses including information on whether model was correct.
    Metrics are also responsible for providing a method for rendering the cached responses as strings.
    This callback then accesses each eval benchmark during eval_end, retrieves the cached results,
    and renders and and logs them in tabular format.

    If print_only_incorrect=False, correct model outputs will be omitted. If subset_sample > 0, then
    only `subset_sample` of the outputs will be logged.
    """

    def __init__(self,
                 print_only_incorrect: bool = False,
                 subset_sample: int = -1,
                 output_directory: Optional[str] = None):
        self.print_only_incorrect = print_only_incorrect
        self.subset_sample = subset_sample
        self.table = {}
        self.output_directory = output_directory if output_directory else os.getcwd()
        self.hash = hashlib.sha256()


    def write_tables_to_output_dir(self, state: State):
        # write tmp files
        self.hash.update((str(time.time()) + str(random.randint(0, 1_000_000))).encode('utf-8'))
        tmp_dir = os.getcwd() + '/' + self.hash.hexdigest()
        if not os.path.exists(tmp_dir):
            os.mkdir(tmp_dir)

        full_df = pd.DataFrame()
        file_name = f"eval-outputs-ba{state.timestamp.batch.value}.tsv"

        print(f"DEBUG: ran all evals, got table keys={self.table.keys()}")
        for benchmark in self.table:
            cols, rows = self.table[benchmark]
            rows = [[e.encode('unicode_escape') if isinstance(e, str) else e for e in row] for row in rows]
            df = pd.DataFrame.from_records(data=rows, columns=cols)
            df['benchmark'] = benchmark
            print(f"DEBUG: got n={len(df)} rows and columns={list(df.columns)}for benchmark={benchmark}")
            full_df = pd.concat([full_df, df], ignore_index=True)
        print(f"DEBUG: got total rows={len(full_df)}")
        
        

        with open(f'{tmp_dir}/{file_name}', 'wb') as f:
            full_df.to_csv(f, sep='\t', index=False)

        # copy/upload tmp files
        _write(destination_path=f'{self.output_directory}/{file_name}', src_file=f'{tmp_dir}/{file_name}')
        os.remove(f'{tmp_dir}/{file_name}')

        # delete tmp files
        os.rmdir(tmp_dir)

    def prep_response_cache(self, state, cache):
        benchmark = state.dataloader_label
        for metric in state.eval_metrics[benchmark].values():
            if hasattr(metric, 'set_response_cache'):
                metric.set_response_cache(cache)

    def eval_start(self, state: State, logger: Logger) -> None:
        self.prep_response_cache(state, True)

    def eval_after_all(self, state: State, logger: Logger) -> None:
        self.write_tables_to_output_dir(state)
        self.table = {}
    
    def eval_standalone_end(self, state: State, logger: Logger) -> None:
        self.write_tables_to_output_dir(state)
        self.table = {}

    def eval_end(self, state: State, logger: Logger) -> None:

        assert state.dataloader is not None
        assert isinstance(state.dataloader, DataLoader)
        if hasattr(state.dataloader, 'dataset') and isinstance(state.dataloader.dataset, ICLDatasetTypes):
            assert isinstance(state.dataloader.dataset, ICLDatasetTypes)
            if hasattr(state.dataloader.dataset, 'tokenizer'):
                tokenizer = state.dataloader.dataset.tokenizer
                benchmark = state.dataloader_label
                print(f"DEBUG: benchmark={benchmark}", flush=True)
                assert benchmark is not None
                assert isinstance(benchmark, str)
                for metric in state.eval_metrics[benchmark].values():
                    if hasattr(metric, 'format_response_cache'):
                        assert isinstance(metric.format_response_cache, Callable)
                        format_response_cache: Callable = metric.format_response_cache
                        columns, rows = format_response_cache(tokenizer)
                        print(f"DEBUG: got n={len(rows)} rows and columns={columns} for benchmark={benchmark}", flush=True)

                        if columns is not None and rows is not None:
                            if 'correct' not in columns:
                                raise ValueError(f"{type(metric)}'s response cache should have column named `correct`")
                            correct_col = columns.index('correct')
                            if self.print_only_incorrect:
                                rows = [r for r in rows if not r[correct_col]]

                            if self.subset_sample > 0:
                                rows = random.sample(rows, min(len(rows), self.subset_sample))

                            for destination in logger.destinations:
                                if not isinstance(destination, ConsoleLogger):
                                    destination.log_table(columns, rows, f'icl_outputs/{benchmark}')

                            self.table[benchmark] = (columns, rows)
        print(f'DEBUG: eval end for benchmark={benchmark}, table has keys {self.table.keys()}')
        self.prep_response_cache(state, False)
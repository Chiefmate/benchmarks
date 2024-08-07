# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Execute a single benchmark method."""
from __future__ import print_function

import datetime
import json
import logging
import os
import time
import traceback

from perfzero.process_info_tracker import ProcessInfoTracker
import perfzero.report_utils as report_utils
from perfzero.tensorflow_profiler import TensorFlowProfiler
import perfzero.utils as utils


def run(benchmark_method, harness_info, site_package_info,
        root_output_dir, config, queue, trial_id):
  try:
    _run_internal(benchmark_method, harness_info, site_package_info,
                  root_output_dir, config, queue, trial_id)
  except Exception:  # pylint: disable=broad-except
    logging.error('Benchmark execution for %s failed due to error:\n %s',
                  benchmark_method, traceback.format_exc())
    queue.put((True, None, False, None))


def _set_file_contents(content_str, output_filename):
  with open(output_filename, 'w') as f:
    f.write(content_str)
  logging.info('Wrote summary to file %s', output_filename)


def _run_internal(benchmark_method, harness_info, site_package_info,
                  root_output_dir, config, queue, trial_id):
  """Run benchmark method and put result to the queue.

  Args:
    benchmark_method: Canonical path to the benchmark method
    harness_info: Description of the benchmark harness used in the benchmark
    site_package_info: Description of the site-package used in the benchmark
    root_output_dir: Directory under which to put the benchmark output
    config: An instance of perfzero_config
    queue: An interprocess queue to transfer benchmark result to the caller.
    trial_id: An integer trial id to annotate in the benchmark result.
  """
  start_timestamp = time.time()
  execution_timestamp = start_timestamp
  method_has_exception = False
  execution_id = (config.execution_id if config.execution_id else
                  datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S-%f'))
  output_dir = os.path.join(root_output_dir, execution_id)
  if config.scratch_gcs_url:
    model_output_dir = os.path.join(config.scratch_gcs_url, execution_id)
  else:
    model_output_dir = output_dir
  utils.make_dir_if_not_exist(output_dir)
  if ':' in benchmark_method:
    benchmark_class, benchmark_method_name = benchmark_method.rsplit(':', 1)
  else:
    benchmark_class, benchmark_method_name = benchmark_method.rsplit('.', 1)
  benchmark_class_name = benchmark_class.rsplit('.', 1)[1]

  tensorflow_profiler = TensorFlowProfiler(
      config.profiler_enabled_time_str, output_dir)
  process_info_tracker = ProcessInfoTracker(output_dir)
  process_info = None

  # Setup per-method file logger
  filehandler = logging.FileHandler(
      filename=os.path.join(output_dir, 'perfzero.log'), mode='w')
  filehandler.setFormatter(
      logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
  logging.getLogger().addHandler(filehandler)

  try:
    if config.tpu_parameters:
      tpu = config.tpu_parameters.get('name')
    else:
      tpu = None
    if config.perfzero_constructor_args:
      constructor_args = json.loads(config.perfzero_constructor_args)
    else:
      constructor_args = {}
    class_instance = utils.instantiate_benchmark_class(
        benchmark_class=benchmark_class,
        output_dir=model_output_dir,
        root_data_dir=config.root_data_dir,
        tpu=tpu,
        constructor_args=constructor_args,
        benchmark_class_type=config.benchmark_class_type)
    # tf.test.Benchmark.report_benchmark() writes results to a file with
    # path benchmark_result_file_path_prefix + benchmark_method
    benchmark_result_file_path_prefix = os.path.join(output_dir, 'proto_')
    os.environ['TEST_REPORT_FILE_PREFIX'] = benchmark_result_file_path_prefix
    benchmark_result_file_path = '{}{}.{}'.format(
        benchmark_result_file_path_prefix,
        benchmark_class_name,
        benchmark_method_name)


    # hhlee -- start
    # e.g. {"cluster":
    #   {"chief":["squad-bert-sync-batch32-chief-0.default.svc:2222"],
    #   "worker":["squad-bert-sync-batch32-worker-0.default.svc:2222","squad-bert-sync-batch32-worker-1.default.svc:2222","squad-bert-sync-batch32-worker-2.default.svc:2222"]},
    # "task":{"type":"chief","index":0},
    # "environment":"cloud"}
    tf_config = json.loads(os.environ.get('TF_CONFIG') or '{}')
    task_config = tf_config.get('task', {})
    task_type = task_config.get('type')
    task_index = task_config.get('index')
    ## network profiling on ps or chief task
    if task_type == 'ps' or task_type == 'chief' or task_type == 'worker':
      os.system("sh /workspace/network.sh &")
      print('========================')
      print('network profile started!')
      print('========================')
    ## gpu profiling on chief or worker task
    if task_type == 'chief' or task_type == 'worker':
      os.system("sh /workspace/gpu.sh &")
    # hhlee -- end
    # Start background threads for profiler and system info tracker
    tensorflow_profiler.start()
    process_info_tracker.start()

    # Run benchmark method
    execution_timestamp = time.time()
    logging.info('Starting benchmark execution: %s', benchmark_method)
    getattr(class_instance, benchmark_method_name)()
    logging.info('Stopped benchmark: %s', benchmark_method)

    # Read and build benchmark results
    raw_benchmark_result = utils.read_benchmark_result(
        benchmark_result_file_path)
    # Explicitly overwrite the name to be the full path to benchmark method
    raw_benchmark_result['name'] = benchmark_method
  except Exception:  # pylint: disable=broad-except
    logging.error('Benchmark execution for %s failed due to error:\n %s',
                  benchmark_method, traceback.format_exc())
    method_has_exception = True
    raw_benchmark_result = {}
    raw_benchmark_result['name'] = benchmark_method
    raw_benchmark_result['wall_time'] = -1
    raw_benchmark_result['extras'] = {}
  finally:
    # Stop background threads for profiler and system info tracker
    process_info = process_info_tracker.stop()
    tensorflow_profiler.stop()
    # hhlee -- start
    # jct in tf_cnn_benchmark = wall_time in perfzero
    model_txt=open('/workspace/model.txt','r')
    save_dir_name=model_txt.read()
    jct=open('/result/'+save_dir_name.strip()+'/'+task_type+'_'+str(task_index)+'_jct.txt','w')
    jct.write('%.2f'%(raw_benchmark_result['wall_time']))
    jct.close()
    model_txt.close()
    # hhlee -- end

  upload_timestamp = time.time()
  benchmark_result = report_utils.build_benchmark_result(
      raw_benchmark_result, method_has_exception, trial_id)
  execution_summary = report_utils.build_execution_summary(
      execution_timestamp,
      execution_id,
      config.ml_framework_build_label,
      config.execution_label,
      config.platform_name,
      config.system_name,
      config.output_gcs_url,
      benchmark_result,
      config.get_env_vars(),
      config.get_flags(),
      harness_info,
      site_package_info,
      process_info,
      method_has_exception,
      is_tpu_benchmark = (config.tpu_parameters != None))
  report_utils.upload_execution_summary(
      config.bigquery_project_name,
      config.bigquery_dataset_table_name,
      execution_summary)
  report_utils.execute_methods(
      config.result_upload_methods,
      execution_summary)
  logging.info('Benchmark execution for %s completed with summary:\n %s',
               benchmark_method, json.dumps(execution_summary, indent=2))
  _set_file_contents(json.dumps(execution_summary, indent=2),
                     os.path.join(output_dir, 'perfzero_summary.json'))
  utils.maybe_upload_to_gcs(output_dir, config.output_gcs_url)
  logging.getLogger().removeHandler(filehandler)
  method_execution_time = {
      'class_initialization': execution_timestamp - start_timestamp,
      'method_execution': upload_timestamp - execution_timestamp,
      'log_upload': time.time() - upload_timestamp
  }

  if config.profiler_enabled_time_str:
    relative_output_dir = output_dir[output_dir.find('benchmark'):]
    print('\nExecute the command below to start tensorboard server using '
          'the collected profiler data:\ntensorboard --logdir={}\n\n'
          'Open localhost:6006 in your browser to access the Tensorbord '
          'GUI. Use ssh with port forwarding if tensorboard is running on '
          'a remote machine.\n'.format(relative_output_dir))

  queue.put((method_has_exception, method_execution_time,
             benchmark_result['succeeded'], output_dir))

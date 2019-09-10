#!/usr/bin/env python3

# Copyright (c) 2019 Trail of Bits, Inc.
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

import argparse
import filecmp
import operator
import os
import queue
import threading
import shutil
import subprocess
from subprocess import CalledProcessError
import sys
import tempfile
import unittest

import tests
import colors
import result_data

llvm_version = 0

lift = None
bin_dir = "bin"
lib_dir = None
recompiled_dir = "recompiled"

batches = []
shared_libs = None
libc = None

def get_recompiled_name(name):
    return name

# Print results of tests
def print_results(t_cases):
    for key, val in sorted(t_cases.items(), key = operator.itemgetter(0)):
        val.print(1)

def log_results(result, into):
    for entry in result.failures:
        what = entry[0]
        into[what.name].cases[what._testMethodName] = result_data.FAIL

    for entry in result.errors:
        what = entry[0]
        into[what.name].cases[what._testMethodName] = result_data.ERROR


# Fill global variables
# TODO: Rework, this is really ugly
def check_arguments(args):
    if args.lift_args and args.lift_args[0] == "--":
        args.lift_args = args.lift_args[1:]

    if not os.path.isfile(args.lift):
        print("{} passed to --lift is not a valid file".format(args.lift))
        sys.exit (1)
    global lift
    lift = args.lift

    global llvm_version
    prefix, sep, llvm_version = lift.rpartition('-')

    if not os.path.isdir(args.lib_dir):
        print ("{} passed to --lib_dir is not a valid directory".format(args.lib_dir))
        sys.exit (1)
    global lib_dir
    lib_dir = args.lib_dir

    if not os.path.isdir(args.bin_dir):
        print("{} passed to --bin_dir is not a valid directory".format(args.bin_dir))
        sys.exit (1)
    bin_dir = os.path.abspath(args.bin_dir)

    if not os.path.isdir(args.shared_lib_dir):
        print("{} passed to --shared_lib_dir is not a valid directory".format(args.shared_lib_dir))
        sys.exit (1)

    global shared_libs
    shared_libs = []
    for lib_name in os.listdir(args.shared_lib_dir):
        shared_libs.append(os.path.join(args.shared_lib_dir, lib_name ))

    print("\n > Shared libraries: ")
    print(shared_libs)

    if not os.path.isdir(args.libc_dir):
        print ("{} passed to --libc_dir is not a valid directory".format(args.libc_dir))
        sys.exit (1)

    global libc
    libc = ''
    for lib_name in os.listdir(args.libc_dir):
        if lib_name.endswith(".bc"):
            libc += os.path.join(args.libc_dir, lib_name + ',')

    print("\n > --abi_libraries files:")
    print(libc)

    # TODO: This whole snippet can be done using argparser and some actions
    if args.batch is not None:
        for batch in args.batch:
            batch_dir = batch + "_cfg"
            if not os.path.isdir(batch_dir):
                print("{} passed to --batch is not a valid directory".format(batch))
                sys.exit(1)
            batches.append(batch_dir)

    if args.batch_dir is not None:
        for batch in args.batch_dir:
            if not os.path.isdir(batch):
                print("{} passed to --batch_dir is not a valid directory".format(batch))
                sys.exit(1)
            batches.append(batch_dir)

    if not batches:
        print("No batch is selected, exiting")
        sys.exit(0)

    print(batches)


def exec_and_log_fail(args):
    pipes = subprocess.Popen(args, stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    std_out, std_err = pipes.communicate()
    ret_code = pipes.returncode

    if ret_code:
        print("** stdout:")
        print(std_out)
        print("** stderr:")
        print(std_err)
        return False
    return True



# Recompile the binary from lifted .bc
# Return None in case error happens
# Otherwise return path to the newly created binary
def build_test(cfg, build_dir, extra_args):
    print("Lifting " + cfg)

    # Create build directory for given test case
    binary_base_name = os.path.splitext(os.path.basename(cfg))[0]
    bc = os.path.join( build_dir, binary_base_name + ".bc")

    # TODO: arch type
    lift_args = [ lift,
                  "-os", "linux",
                  "-arch", "amd64",
                  "-cfg", cfg,
                  "-abi_libraries", libc,
                  "-output", bc ]
    lift_args += extra_args

    if not exec_and_log_fail(lift_args):
        return None

    # Recompile it
    lifted = os.path.join(build_dir, get_recompiled_name(binary_base_name))
    lib = lib_dir + "/"+ "libmcsema_rt64-6.0.a"


    recompile_args =[ "clang-{}".format(llvm_version), bc,
                      "-o", lifted, lib,
                      "-lpthread", "-lm", "-ldl"] + shared_libs
    if not exec_and_log_fail(recompile_args):
        return None
    return lifted


def thread_lift(*t_args, **kwargs):
    todo, suite_cases, test_dir, args = t_args
    while not todo.empty():

        try:
            batch, f = todo.get()
        except queue.Empty:
            return

        print("\n > Handling : " + f)
        recompiled = build_test(os.path.join(batch, f), test_dir, args.lift_args )

        basename = os.path.splitext(f)[0]
        tc = result_data.TCData(basename,
                    os.path.join(os.getcwd(), os.path.join(bin_dir, basename)),
                    recompiled)
        tests.BaseTest.cases[basename] = tc

        # Lift failed for some reason, ignore this case
        if recompiled is None:
            continue

        # Dynamically load only test cases for binaries that were successfully lifted
        # Therefore ignored are fails and binaries not present in a batch
        # TODO: Solve missing test case class
        suite_name = basename + "_suite"

        try:
            tc_class = tests.__dict__[suite_name]
            loader = unittest.TestLoader()
            suite_cases.put_nowait(item=loader.loadTestsFromTestCase(tc_class))
        except KeyError:
            print(" > " + suite_name + " was not found in module!")
            print(colors.bg_yellow(" > Skipping test"))
            continue
    return


# Right now batches are combined, maybe it would make sense to separate batches from each other
# that can be useful when comparing performance of frontends
def main():
    arg_parser = argparse.ArgumentParser(
        formatter_class = argparse.RawDescriptionHelpFormatter)

    arg_parser.add_argument('--lift',
                            help = "Path to the mcsema-lift binary",
                            required = True)

    arg_parser.add_argument('--lib_dir',
                            help = "Directory that contains libmcsema_rt64.a",
                            required = True)

    arg_parser.add_argument('--shared_lib_dir',
                            help = "Directory that contains shared libraries used by original binaries",
                            default = "shared_libs",
                            required = False)


    arg_parser.add_argument('--bin_dir',
                            help = "Directory that contains original binaries",
                            default = "bin",
                            required = False)


    arg_parser.add_argument('--batch',
                            help = "Batch names",
                            nargs = '+',
                            required = False)

    arg_parser.add_argument('--batch_dir',
                            help = 'Directories with cfgs',
                            nargs = '+',
                            required = False)

    arg_parser.add_argument('--libc_dir',
                            help = "Directory that contains bitcode types of external functions",
                            required = True)

    arg_parser.add_argument('--dry_run',
                            help = 'Do not run any tests',
                            required = False)

    arg_parser.add_argument('--save_log',
                            help = "Name of file to save result in json format",
                            required = False)

    arg_parser.add_argument('--jobs',
                            help = "Number of threads to use",
                            default = 1,
                            required = False)

    arg_parser.add_argument('lift_args',
                            help = "Additional arguments passed to mcsema-lift",
                            nargs = argparse.REMAINDER)


    args, command_args = arg_parser.parse_known_args()
    check_arguments(args)

    tests.init()

    # Create directory to store recompiled binaries
    # TemporaryDirectory() is not used, since we may want to have a look at recompiled code,
    # maybe we want some --preserve option
    test_dir = tempfile.mkdtemp(dir=os.getcwd())

    suite_cases = queue.Queue()
    todo = queue.Queue()

    for batch in batches:
        for f in os.listdir(batch):
            todo.put((batch, f))

    threads = []
    for i in range(int(args.jobs)):
        t = threading.Thread(target=thread_lift, args=(todo, suite_cases, test_dir, args))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print()
    suite = unittest.TestSuite(list(suite_cases.queue))
    result = unittest.TextTestRunner(verbosity = 0).run(suite)

    log_results(result, tests.BaseTest.cases)
    print_results(tests.BaseTest.cases)

    log_file = args.save_log
    if log_file is not None:
        result_data.store_json(tests.BaseTest.cases, log_file)

if __name__ == '__main__':
    main()

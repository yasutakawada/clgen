#
# Copyright 2016 Chris Cummins <chrisc.101@gmail.com>.
#
# This file is part of CLgen.
#
# CLgen is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CLgen is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CLgen.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Preprocess OpenCL files for machine learning
"""
from __future__ import with_statement
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import math
import os
import re
import shutil
import sqlite3
import sys

from functools import partial
from hashlib import md5
from io import open
from multiprocessing import cpu_count, Pool
from subprocess import Popen, PIPE, STDOUT
from tempfile import NamedTemporaryFile

import labm8
from labm8 import fs

import clgen
from clgen import clutil
from clgen import log
from clgen import native
from clgen.cache import Cache


#
# Custom exceptions:
#

# Internal exceptions:
class LlvmException(clgen.CLgenError): pass
class ClangFormatException(LlvmException): pass
class OptException(LlvmException): pass

# Good, bad, ugly exceptions:
class BadCodeException(clgen.CLgenError): pass
class ClangException(BadCodeException): pass
class CodeAnalysisException(BadCodeException): pass

class UglyCodeException(clgen.CLgenError): pass
class InstructionCountException(UglyCodeException): pass
class RewriterException(UglyCodeException): pass


CLANG_CL_TARGETS = [
    'nvptx64-nvidia-nvcl',
    'spir64'
]


def clang_cl_args(target=CLANG_CL_TARGETS[0],
                  error_limit=0):
    """
    Get the Clang args to compile OpenCL.

    :return: Array of args.
    """
    # List of clang warnings to disable.
    disabled_warnings = [
        'ignored-pragmas',
        'implicit-function-declaration',
        'incompatible-library-redeclaration',
        'macro-redefined',
    ]

    return [
        '-I' + fs.path(native.LIBCLC),
        '-include', native.SHIMFILE,
        '-target', target,
        '-ferror-limit={}'.format(error_limit),
        '-xcl'
    ] + ['-Wno-{}'.format(x) for x in disabled_warnings]


def num_rows_in(db, table):
    c = db.cursor()
    c.execute('SELECT Count(*) FROM ' + str(table))
    num_rows = c.fetchone()[0]
    c.close()
    return num_rows


def compiler_preprocess_cl(src, id='anon'):
    cmd = [native.CLANG] + clang_cl_args() + [
        '-E', '-c', '-', '-o', '-'
    ]
    process = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate(src.encode('utf-8'))

    if process.returncode != 0:
        raise ClangException(stderr.decode('utf-8'))

    src = stdout.decode('utf-8')
    lines = src.split('\n')

    # Strip all the includes:
    for i, line in enumerate(lines):
        if line == '# 1 "<stdin>" 2':
            break
    src = '\n'.join(lines[i + 1:]).strip()

    # Strip lines beginning with '#' (that's preprocessor
    # stuff):
    src = '\n'.join([line for line in src.split('\n')
                     if not line.startswith('#')])

    return src


def rewrite_cl(src, id='anon'):
    # Rewriter can't read from stdin.
    with NamedTemporaryFile('w', suffix='.cl') as tmp:
        tmp.write(src)
        tmp.flush()
        cmd = ([native.CLGEN_REWRITER, tmp.name] +
               ['-extra-arg=' + x for x in clang_cl_args()] + ['--'])

        process = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = process.communicate()

    # If there was nothing to rewrite, rewriter exits with error code:
    EUGLY_CODE = 204
    if process.returncode == EUGLY_CODE:
        # Propagate the error:
        raise RewriterException(src)
    # NOTE: the rewriter process can still fail because of some other
    # compilation problem, e.g. for some reason the 'enable 64bit
    # support' pragma which should be included in the shim isn't being
    # propogated correctly to the rewriter. However, the rewriter will
    # still correctly process the input, so we ignore all error codes
    # except the one we care about (EUGLY_CODE).
    rewritten = stdout.decode('utf-8')

    # Remove __attribute__ qualifiers
    stripped = clutil.strip_attributes(rewritten)

    return stripped


def compile_cl_bytecode(src, id='anon'):
    cmd = [native.CLANG] + clang_cl_args() + [
        '-emit-llvm', '-S', '-c', '-', '-o', '-'
    ]

    process = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate(src.encode('utf-8'))

    if process.returncode != 0:
        raise ClangException(stderr.decode('utf-8'))
    return stdout


_instcount_re = re.compile(
    r"^(?P<count>\d+) instcount - Number of (?P<type>.+)")


def parse_instcounts(txt):
    lines = [x.strip() for x in txt.split("\n")]
    counts = {}

    # Build a list of counts for each type.
    for line in lines:
        match = re.search(_instcount_re, line)
        if match:
            count = int(match.group("count"))
            key = match.group("type")
            if key in counts:
                counts[key].append(count)
            else:
                counts[key] = [count]

    # Sum all counts.
    for key in counts:
        counts[key] = sum(counts[key])

    return counts


_sql_rm_chars = re.compile(r'[\(\)]')
_sql_sub_chars = re.compile(r'-')


def escape_sql_key(key):
    return re.sub(_sql_sub_chars, '_',
                  re.sub(_sql_rm_chars, '', '_'.join(key.split(' '))))


def instcounts2ratios(counts):
    if not len(counts):
        return {}

    ratios = {}
    total_key = "instructions (of all types)"
    non_ratio_keys = [
        total_key
    ]
    total = float(counts[total_key])

    for key in non_ratio_keys:
        ratios[escape_sql_key(key)] = counts[key]

    for key in counts:
        if key not in non_ratio_keys:
            # Copy count
            ratios[escape_sql_key(key)] = counts[key]
            # Insert ratio
            ratios[escape_sql_key('ratio_' + key)] = float(counts[key]) / total

    return ratios


def sql_insert_dict(c, table, data):
    cmd = ("INSERT INTO {table}({cols}) VALUES({vals})"
           .format(table=table,
                   cols=','.join(data.keys()),
                   vals=','.join(['?'] * len(data))))

    c.execute(cmd, tuple(data.values()))


def bytecode_features(bc, id='anon'):
    cmd = [native.OPT, '-analyze', '-stats', '-instcount', '-']

    # LLVM pass output pritns to stderr, so we'll pipe stderr to
    # stdout.
    process = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    stdout, _ = process.communicate(bc)

    if process.returncode != 0:
        raise OptException(stdout.decode('utf-8'))

    instcounts = parse_instcounts(stdout.decode('utf-8'))
    instratios = instcounts2ratios(instcounts)

    return instratios

# Options to pass to clang-format.
#
# See: http://clang.llvm.org/docs/ClangFormatStyleOptions.html
#
clangformat_config = {
    'BasedOnStyle': 'Google',
    'ColumnLimit': 500,
    'IndentWidth': 2,
    'AllowShortBlocksOnASingleLine': False,
    'AllowShortCaseLabelsOnASingleLine': False,
    'AllowShortFunctionsOnASingleLine': False,
    'AllowShortLoopsOnASingleLine': False,
    'AllowShortIfStatementsOnASingleLine': False,
    'DerivePointerAlignment': False,
    'PointerAlignment': 'Left'
}


def clangformat_ocl(src, id='anon'):
    cmd = [native.CLANG_FORMAT, '-style={}'.format(
        json.dumps(clangformat_config))]
    process = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate(src.encode('utf-8'))

    if stderr:
        log.error(stderr.decode('utf-8'))
    if process.returncode != 0:
        raise ClangFormatException(stderr.decode('utf-8'))

    return stdout.decode('utf-8')


def print_bytecode_features(db_path):
    db = sqlite3.connect(db_path)
    c = db.cursor()

    c.execute('SELECT sha,contents FROM Bytecodes')
    query = c.fetchall()

    uniq_features = set()
    for row in query:
        sha, contents = row

        features = bytecode_features(contents)
        # Add the table key
        features['sha'] = sha
        for key in features.keys():
            uniq_features.add(key)

    log.info('Features:')
    for feature in uniq_features:
        log.info('        ', feature)


def verify_bytecode_features(bc_features, id='anon'):
    # The minimum number of instructions before a kernel is discarded
    # as ugly.
    min_num_instructions = 0
    try:
        num_instructions = bc_features['instructions_of_all_types']
    except KeyError:
        num_instructions = 0

    if num_instructions < min_num_instructions:
        raise InstructionCountException(
            'Code contains {} instructions. The minimum allowed is {}'
            .format(num_instructions, min_num_instructions))


def sanitize_prototype(src):
    # Ensure that prototype is well-formed on a single line:
    try:
        prototype_end_idx = src.index('{') + 1
        prototype = ' '.join(src[:prototype_end_idx].split())
        return prototype + src[prototype_end_idx:]
    except ValueError:
        # Ok so erm... if the '{' character isn't found, a ValueError
        # is thrown. Why would '{' not be found? Who knows, but
        # whatever, if the source file got this far through the
        # preprocessing pipeline then it's clearly "good" code. It
        # could just be that an empty file slips through the cracks or
        # something.
        return src


# 3 possible outcomes:
#
#   1. Good. Code is preprocessed and ready to be put into a training set.
#   2. Bad. Code can't be preprocessed.
#   3. Ugly. Code can be preprocessed, but isn't useful for training.
#
def preprocess(src, id='anon'):
    """
    Preprocess an OpenCL source. There are three possible outcomes:

    1. Good. Code is preprocessed and ready to be put into a training set.
    2. Bad. Code can't be preprocessed (i.e. it's "bad" OpenCL).
    3. Ugly. Code can be preprocessed but isn't useful for training
       (e.g. it's an empty file).

    :param src: The source code as a string.
    :param id (optional): An identifying name for the source code
      (used in exception messages).
    :return: Preprocessed source code as a string.
    :throws BadCodeException: If code is bad (see above).
    :throws UglyCodeException: If code is ugly (see above).
    :throws clgen.InternalException: In case of some other error.
    """
    # Compile to bytecode and verify features:
    bc = compile_cl_bytecode(src, id)
    bc_features = bytecode_features(bc, id)
    verify_bytecode_features(bc_features, id)

    # Rewrite and format source:
    src = compiler_preprocess_cl(src, id)
    src = rewrite_cl(src, id)
    src = clangformat_ocl(src, id).strip()
    src = sanitize_prototype(src)

    return src


class md5sum_aggregator:
    def __init__(self):
        self.md5 = md5()

    def step(self, value):
        self.md5.update(str(value).encode('utf-8'))

    def finalize(self):
        return self.md5.hexdigest()


class linecount_aggregator:
    def __init__(self):
        self.count = 0

    def step(self, value):
        self.count += len(value.split('\n'))

    def finalize(self):
        return self.count


class charcount_aggregator:
    def __init__(self):
        self.count = 0

    def step(self, value):
        self.count += len(value)

    def finalize(self):
        return self.count


def is_modified(db):
    c = db.cursor()

    c.execute("SELECT value FROM Meta WHERE key='preprocessed_checksum'")
    result = c.fetchone()
    cached_checksum = result[0] if result else None

    c.execute('SELECT MD5SUM(id) FROM ContentFiles')
    checksum = c.fetchone()[0]
    c.close()

    return False if cached_checksum == checksum else checksum


def set_modified_status(db, checksum):
    c = db.cursor()
    c.execute("INSERT OR REPLACE INTO Meta VALUES (?,?)",
              ('preprocessed_checksum', checksum))
    db.commit()
    c.close()


def _preprocess_db_worker(job):
    """Database worker thread"""
    db_path = job["db_in"]
    db_index_range = job["db_index_range"]
    outpath = job["json_out"]
    log.debug("worker", outpath)

    db = sqlite3.connect(db_path)
    c = db.cursor()
    split_start, split_end = db_index_range
    split_size = split_end - split_start

    # get the files to preprocess
    c.execute('SELECT id,contents FROM ContentFiles LIMIT {} OFFSET {}'
              .format(split_size, split_start))

    with open(outpath, 'wb') as outfile:
        for row in c.fetchall():
            id, contents = row

            # Get checksum of cached file:
            c.execute('SELECT id FROM PreprocessedFiles WHERE id=?', (id,))
            result = c.fetchone()
            cached_id = result[0] if result else None

            # Check that file is modified:
            if id != cached_id:
                try:
                    # Try and preprocess it:
                    contents = preprocess(contents, id)
                    status = 0
                except BadCodeException as e:
                    contents = str(e)
                    status = 1
                except UglyCodeException as e:
                    contents = str(e)
                    status = 2

                # write result to json
                line = json.dumps([id, status, contents]).encode('utf-8')
                outfile.write(line)
                outfile.write('\n')

    c.close()
    db.close()


def preprocess_contentfiles(db_path, max_num_workers=cpu_count() * 4):
    def _finalize(db_path, cache):
        """Tidy up after worker threads finish"""
        log.debug("worker finalize")

        db = sqlite3.connect(db_path)
        c = db.cursor()

        # import results from worker threads
        for outpath in fs.ls(cache.path, abspaths=True):
            with open(outpath) as infile:
                for line in infile:
                    c.execute('INSERT OR REPLACE INTO PreprocessedFiles '
                              'VALUES(?,?,?)', json.loads(line))

        # write changes to database and remove cache
        db.commit()
        db.close()
        cache.empty()

    db = sqlite3.connect(db_path)
    num_contentfiles = num_rows_in(db, 'ContentFiles')
    num_preprocessedfiles = num_rows_in(db, 'PreprocessedFiles')
    db.close()

    num_workers = min(num_contentfiles, max_num_workers)
    files_per_worker = math.ceil(num_contentfiles / num_workers)

    # temporary cache used for worker thread results
    cache = Cache("{pid}.preprocess".format(pid=os.getpid()))
    # each worker thread receives a range of database indices to preprocess,
    # and a JSON file to write results into
    jobs = [{
        "db_in": db_path,
        "db_index_range": (i * files_per_worker,
                           i * files_per_worker + files_per_worker),
        "json_out": fs.path(cache.path, "{i}.json".format(i=i))
    } for i in range(num_workers)]

    # spool up worker threads then finalize
    try:
        log.info('spawning', num_workers, 'worker threads to process',
                 num_contentfiles - num_preprocessedfiles, 'files ...')
        with clgen.terminating(Pool(num_workers)) as pool:
            pool.map(_preprocess_db_worker, jobs)
    except Exception as e:
        _finalize(db_path, cache)
        raise e
    _finalize(db_path, cache)


def preprocess_file(path, inplace=False):
    """
    Preprocess a file.

    :param path: String path to file.
    :param inplace (optional): If True, overwrite input file.
    """
    with open(path) as infile:
        contents = infile.read()
    try:
        out = preprocess(contents)
        if inplace:
            with open(path, 'w') as outfile:
                outfile.write(out)
        else:
            log.info('preprocess', out)
    except BadCodeException as e:
        log.fatal(e, ret=1)
    except UglyCodeException as e:
        log.fatal(e, ret=2)


def _preprocess_inplace_worker(path):
    """
    Worker function for preprocess_inplace().
    """
    log.info('preprocess', path)
    preprocess_file(path, inplace=True)


def preprocess_inplace(paths, max_num_workers=cpu_count() * 4):
    """
    Preprocess a list of files inplace.
    """
    num_workers = min(len(paths), max_num_workers)
    with clgen.terminating(Pool(num_workers)) as pool:
        log.info('spawning', num_workers, 'worker threads to process',
                 len(paths), 'files ...')
        pool.map(_preprocess_inplace_worker, paths)


def connect(db_path):
    """
    Returns a connection to a database.

    Database has additional aggregate functions:

        MD5SUM() returns md5 of column values
        LC() returns sum line count of text columns
        CC() returns sum character count of text columns

    Arguments:

        db_path (str): Path to database

    Returns:

        sqlite3 connection
    """
    db = sqlite3.connect(db_path)
    db.create_aggregate("MD5SUM", 1, md5sum_aggregator)
    db.create_aggregate("LC", 1, linecount_aggregator)
    db.create_aggregate("CC", 1, charcount_aggregator)
    return db


def preprocess_db(db_path):
    """
    Preprocess database contents.

    Arguments:

        db_path (str): Path to database.

    Returns:

        bool: True if modified, false if no work needed.
    """
    db = connect(db_path)

    modified = is_modified(db)
    if modified:
        preprocess_contentfiles(db_path)
        set_modified_status(db, modified)
        return True
    else:
        return False


def remove_bad_preprocessed(db_path):
    """
    Remove all ugly and bad contents from PreprocessedFiles table.
    """
    original_size = fs.du(db_path, human_readable=False)
    original_size_human_readable = fs.du(db_path, human_readable=True)
    log.info("vacuuming", original_size_human_readable, "database")
    sys.stdout.flush()

    # Remove contents from bad or ugly preprocessed files.
    db = sqlite3.connect(db_path)
    c = db.cursor()
    c.execute("UPDATE PreprocessedFiles SET contents='[DELETED]' "
              "WHERE status=1 OR status=2")
    db.commit()
    c.close()

    c = db.cursor()
    c.execute("VACUUM")
    db.commit()
    c.close()

    new_size = fs.du(db_path, human_readable=False)
    new_size_human_readable = fs.du(db_path, human_readable=True)
    reduction_ratio = (1 - (new_size / original_size)) * 100
    log.info("done. new size {}. ({:.0f}% reduction)"
             .format(new_size_human_readable, reduction_ratio), sep=".")
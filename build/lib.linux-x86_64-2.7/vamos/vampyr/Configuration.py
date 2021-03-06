
"""utility classes for working in source trees"""

# Copyright (C) 2011 Christian Dietrich <christian.dietrich@informatik.uni-erlangen.de>
# Copyright (C) 2011-2012 Reinhard Tartler <tartler@informatik.uni-erlangen.de>
# Copyright (C) 2012 Christoph Egger <siccegge@informatik.uni-erlangen.de>
# Copyright (C) 2012 Manuel Zerpies <manuel.f.zerpies@ww.stud.uni-erlangen.de>
# Copyright (C) 2012-2014 Stefan Hengelein <stefan.hengelein@fau.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import vamos.golem.kbuild as kbuild
import vamos.tools as tools
import vamos.model as Model
from vamos.Config import Config
from vamos.vampyr.Messages import SparseMessage, GccMessage, ClangMessage, SpatchMessage

import logging
import os.path
import re
import shutil

class CheckerNotImplemented(RuntimeError):
    pass

class Configuration:
    def __init__(self, framework, basename, nth):
        self.cppflags = '%s.cppflags%s' % (basename, nth)
        self.source   = '%s.source%s' % (basename, nth)
        self.kconfig  = '%s.config%s' % (basename, nth)
        # Note, undertaker will clean up ".config*", which matches self.config_h
        self.config_h = '%s.config%s.h' % (basename, nth)
        self.framework = framework
        self.basename = basename
        self.write_config_h(self.config_h)

    def write_config_h(self, config_h):
        with open(config_h, 'w') as fd:
            cppflags = self.get_cppflags()
            flags = cppflags.split("-D")
            logging.debug("Generating %s, found %d items",
                          config_h, len(flags))
            for f in flags:
                if f == '': continue # skip empty fields
                try:
                    name, value = f.split('=')
                    fd.write("#define %s %s\n" % (name, value))
                except ValueError:
                    logging.error("%s: Failed to parse flag '%s'", config_h, f)

    def switch_to(self):
        raise NotImplementedError

    def get_cppflags(self):
        with open(self.cppflags, 'r') as fd:
            return fd.read().strip()

    def filename(self):
        return self.kconfig

    def __copy__(self):
        raise RuntimeError("Object <%s> is not copyable" % self)

    # deepcopy takes a memo-dict as parameter which stores which objects are already copied
    # https://docs.python.org/3/library/copy.html?highlight=deepcopy#copy.deepcopy
    def __deepcopy__(self, memo):
        raise RuntimeError("Object <%s> is not copyable" % self)

    def get_config_h(self):
        """
        Returns the path to a config.h like configuration file
        """
        return self.config_h


class BareConfiguration(Configuration):

    def __init__(self, framework, basename, nth):
        Configuration.__init__(self, framework, basename, nth)

    def __repr__(self):
        return '"' + self.get_cppflags() + '"'

    def switch_to(self):
        logging.debug("nothing to do for switching to %s", self)

    def __call_compiler(self, compiler, args, on_file):
        cmd = compiler + " " + args
        if compiler in self.framework.options['args']:
            cmd += " " + self.framework.options['args'][compiler]
        cmd += " " + self.get_cppflags()
        cmd += " '" + on_file + "'"
        (out, returncode) = tools.execute(cmd, failok=True)
        if returncode == 127:
            raise RuntimeError(compiler + " not found on this system?")
        else:
            return (out, returncode)

    def call_sparse(self, on_file):
        (messages, statuscode) = self.__call_compiler("sparse", "", on_file)
        if statuscode != 0:
            messages.append(on_file +
                            ":1: error: cannot compile file [ARTIFICIAL, rc=%d]" % statuscode)

        messages = SparseMessage.preprocess_messages(messages)
        messages = [SparseMessage(self, x) for x in messages]
        return messages


    def call_gcc(self, on_file):
        (messages, statuscode) = self.__call_compiler("gcc", "-o/dev/null -c", on_file)
        if statuscode != 0:
            messages.append(on_file +
                            ":1: error: cannot compile file [ARTIFICIAL, rc=%d]" % statuscode)

        messages = GccMessage.preprocess_messages(messages)
        messages = [GccMessage(self, x) for x in messages]
        return messages


    def call_clang(self, on_file):
        (messages, statuscode) = self.__call_compiler("clang", "--analyze", on_file)

        if statuscode != 0:
            messages.append(on_file +
                            ":1: error: cannot compile file [ARTIFICIAL, rc=%d]" % statuscode)

        messages = ClangMessage.preprocess_messages(messages)
        messages = [ClangMessage(self, x) for x in messages]
        return messages

    def expand(self, verify=True):
        pass

    def call_spatch(self, on_file):
        messages = []
        for test in self.framework.options['test']:
            (out, _) = self.__call_compiler("spatch", "-sp_file %s" % test, self.source)

            if len(out) > 1 or out[0] != '':
                out = SpatchMessage.preprocess_messages(out)
                messages += [SpatchMessage(self, x, on_file, test) for x in out]

        return messages


class KbuildConfiguration(Configuration):
    """A common base class for kbuild-like projects

    The expand method uses Kconfig to fill up the remaining
    variables. The parameter expansion_strategy of the __init__ method
    selects how partial configurations get expanded. In the default
    mode, 'alldefconfig', the strategy is to set the remaining variables
    to their Kconfig defined defaults. With 'allnoconfig', the strategy
    is to enable as few features as possible.

    The attribute 'model' of this class is allocated in the expand()
    method on demand.

    """
    def __init__(self, framework, basename, nth):
        Configuration.__init__(self, framework, basename, nth)

        self.expanded = None
        self.model = None
        self.arch = self.framework.options['arch']
        self.subarch = self.framework.options['subarch']
        self.result_cache = {}

        if self.framework.options.has_key('expansion_strategy'):
            self.expansion_strategy = self.framework.options['expansion_strategy']
        else:
            self.expansion_strategy = 'alldefconfig'

        try:
            os.unlink(self.kconfig + '.expanded')
        except OSError:
            pass

    def get_config_h(self):
        return self.framework.find_autoconf()

    def __repr__(self):
        raise NotImplementedError

    def call_makefile(self, target, extra_variables="", extra_env="", failok=False):
        return self.framework.call_makefile(target,
                                            extra_variables=extra_variables,
                                            extra_env=extra_env,
                                            failok=failok)

    def expand(self, verify=False):
        """
        @raises ExpansionError if verify=True and expanded config does
                               not patch all requested items
        """
        logging.debug("Trying to expand configuration " + self.kconfig)

        if not os.path.exists(self.kconfig):
            raise RuntimeError("Partial configuration %s does not exist" % self.kconfig)

        files = self.framework.cleanup_autoconf_h()

        if len(files) > 1:
            logging.error("Deleted spurious configuration files: %s", ", ".join(files))

        extra_env = 'KCONFIG_ALLCONFIG="%s"' % self.kconfig
        self.call_makefile(self.expansion_strategy, extra_env=extra_env)
        self.framework.apply_configuration()

        self.expanded = self.save_expanded('.config')

        if verify:
            modelf = Model.get_model_for_arch(self.arch)
            if not modelf:
                logging.error("Skipping verification as no model could be loaded")
                return

            if not self.model:
                self.model = Model.parse_model(modelf)
                logging.info("Loaded %d items from %s", len(self.model), modelf)

            all_items, violators = self.verify(self.expanded)
            if len(violators) > 0:
                logging.warning("%d/%d items differ in expanded configuration", len(violators), len(all_items))
                for v in violators:
                    logging.warning(" item: %s", v)
            else:
                logging.info("All items are set correctly")

    def save_expanded(self, config):
        expanded_config = self.kconfig + '.expanded'
        shutil.copy(config, expanded_config)
        return expanded_config

    def get_expanded(self):
        """if already expanded, returns the path to the file that holds the expanded configuration

        returns None otherwise
        """
        expanded_config = self.kconfig + '.expanded'
        if os.path.exists(expanded_config):
            return expanded_config
        else:
            return None

    def expand_stdconfig(self):
        expanded = self.get_expanded()

        if expanded:
            shutil.copy(expanded, '.config')
        else:
            stdconfig = self.framework.options['stdconfig']
            self.call_makefile(stdconfig, failok=False)

            # mark this configuration as already expanded, now that we have saved it
            self.expanded = self.save_expanded('.config')
            shutil.copy(self.expanded, self.kconfig)

        self.framework.apply_configuration()

        if not self.framework.options.has_key('stdconfig_files'):
            self.framework.options['stdconfig_files'] \
                = set(kbuild.files_for_current_configuration(self.arch, self.subarch))

    def verify(self, expanded_config='.config'):
        """
        verifies that the given expanded configuration satisfies the
        constraints of the given partial configuration.

        @return (all_items, violators)
          all_items: set of all items in partial configuration
          violators: list of items that violate the partial selection
        """

        partial_config = Config(self.kconfig)
        config = Config(expanded_config)
        conflicts = config.getConflicts(partial_config)

        return (partial_config.keys(), conflicts)

    def switch_to(self):
        logging.info("Switching to configuration %s", self)

        # sanity check: remove existing configuration to ensure consistent behavior
        if os.path.exists(".config"):
            os.unlink(".config")

        if self.expanded is None:
            logging.debug("Expanding partial configuration %s", self.kconfig)
            self.expand()
        else:
            # now replace the old .config with our 'expanded' one
            shutil.copyfile(self.expanded, '.config')
            self.framework.apply_configuration()

        assert os.path.exists('.config')


    def call_make(self, on_file, extra_args):
        on_object = on_file[:-1] + "o"

        # dry compilation to ensure all dependent objects are present,
        # but only if we are actually interested in the compiler output
        if not 'CHECK=' in extra_args:
            self.call_makefile(on_object, failok=True, extra_variables=extra_args)

        if os.path.exists(on_object):
            os.unlink(on_object)

        try:
            cmd = None
            (messages, statuscode) = \
                self.call_makefile(on_object, failok=False, extra_variables=extra_args)
        except tools.CommandFailed as e:
            statuscode, cmd, messages = e.returncode, e.command, e.stdout

        state = None
        CC = []
        CHECK = []

        while len(messages) > 0:
            if re.match(r"^\s*CC\s*(\[M\]\s*)?" + on_object, messages[0]):
                state = "CC"
                del messages[0]
                continue
            if re.match(r"^\s*CHECK\s*(\[M\]\s*)?" + on_file, messages[0]):
                state = "CHECK"
                del messages[0]
                continue
            if re.match(r"fixdep: [\S]* is empty", messages[0]):
                del messages[0]
                continue

            # Skip lines before "    CC"
            if state == None:
                pass
            elif state == "CC":
                CC.append(messages[0])
            elif state == "CHECK":
                CHECK.append(messages[0])
            else:
                raise RuntimeError("Should never been reached")

            # Remove line
            del messages[0]

        if statuscode != 0:
            logging.error("Running checker %s on file %s failed", cmd, on_file)
            logging.error("contents of CC:")
            logging.error(CC)
            logging.error("contents of CHECK:")
            logging.error(CHECK)

        return (CC, CHECK)

    def call_gcc(self, on_file):
        """Call Gcc on the given file"""
        if "CC" in self.result_cache:
            return self.result_cache["CC"]

        extra_args = "KCFLAGS='%s'" % self.framework.options['args']['gcc']
        if self.framework.options.has_key('cross_prefix') and \
                len(self.framework.options['cross_prefix']) > 0:
            extra_args += " CROSS_COMPILE=%s" % self.framework.options['cross_prefix']
        (CC, _) = self.call_make(on_file, extra_args)

        messages = GccMessage.preprocess_messages(CC)
        messages = [GccMessage(self, x) for x in messages]
        self.result_cache["CC"] = messages
        return messages


    def call_sparse(self, on_file):
        """Call Sparse on the given file"""
        if "SPARSE" in self.result_cache:
            return self.result_cache["SPARSE"]

        sparse = "ulimit -t 30; sparse"
        if 'sparse' in self.framework.options['args']:
            sparse += " " + self.framework.options['args']['sparse']

        (CC, CHECK) = self.call_make(on_file, "C=2 CC='fakecc' CHECK='%s'" % sparse.replace("'", "\\'"))

        # GCC messages
        messages = GccMessage.preprocess_messages(CC)
        messages = [GccMessage(self, x) for x in messages]
        self.result_cache["CC"] = messages

        # Sparse messages
        messages = SparseMessage.preprocess_messages(CHECK)
        messages = [SparseMessage(self, x) for x in messages]
        self.result_cache["SPARSE"] = messages

        return messages

    def call_spatch(self, on_file):
        """Call Spatch on the given file"""
        if "SPATCH" in self.result_cache:
            return self.result_cache["SPATCH"]

        messages = []

        for test in self.framework.options['test']:
            spatch = 'vampyr-spatch-wrapper "%s" "%s" -sp_file "%s"' % (on_file, self.source, test)
            if 'spatch' in self.framework.options['args']:
                spatch += " " + self.framework.options['args']['spatch']

            (CC, CHECK) = self.call_make(on_file, "C=2 CHECK='%s' CC=fakecc" % spatch.replace("'", "\\'"))

            # GCC messages
            if "CC" not in self.result_cache:
                messages = GccMessage.preprocess_messages(CC)
                messages = [GccMessage(self, x) for x in messages]
                self.result_cache["CC"] = messages

            if len(CHECK) > 1 or (len(CHECK) > 0 and CHECK[0] != ''):
                # Sparse messages
                out = SpatchMessage.preprocess_messages(CHECK)
                messages += [SpatchMessage(self, x, on_file, test) for x in out]

        self.result_cache["SPATCH"] = messages
        return messages


class LinuxConfiguration(KbuildConfiguration):
    """
    This class represents a (partial) Linux configuration.

    The expand method uses Kconfig to fill up the remaining
    variables. The field expansion_strategy of the framework's option
    dict selects how partial configurations get expanded. In the default
    mode, 'alldefconfig', the strategy is to set the remaining variables
    to their Kconfig defined defaults. With 'allnoconfig', the strategy
    is to enable as few features as possible.

    The attribute 'model' of this class is allocated in the expand()
    method on demand.

    """
    def __init__(self, framework, basename, nth):
        KbuildConfiguration.__init__(self, framework, basename, nth)
        self.arch    = self.framework.options['arch']
        self.subarch = self.framework.options['subarch']

    def __repr__(self):
        return '<LinuxConfiguration "' + self.kconfig + '">'


class LinuxPartialConfiguration(LinuxConfiguration):
    """
    This class creates a configuration object for a partial Linux
    Configuration. This works on arbitrary partial configurations, like
    "trolled" ones.

    NB: the self.cppflags and self.source is set to "/dev/null"
    """

    def __init__(self, framework, filename, arch=None, subarch=None):
        LinuxConfiguration.__init__(self, framework,
                                    basename=filename, nth="")

        self.cppflags = '/dev/null'
        self.source   = '/dev/null'
        self.kconfig  = filename

        if arch and subarch:
            self.arch, self.subarch = arch, subarch
        else:
            self.arch, self.subarch = kbuild.guess_arch_from_filename(filename)

    def write_config_h(self, dummy):
        pass

    def filename(self):
        return self.basename

    def call_makefile(self, target, extra_variables="", extra_env="", failok=False):
        # do not use the architecture set from framework, but the possibly
        # overriden one from the configuration. (i.e., handle subarch changes gracefully)
        return kbuild.call_linux_makefile(target, extra_variables=extra_variables,
                                          arch=self.arch, subarch=self.subarch,
                                          failok=failok)


class LinuxStdConfiguration(LinuxConfiguration):
    """
    This class creates a configuration object for a standard Linux
    configuration, such as 'allyesconfig' or 'allnoconfig'.

    Instantiating this class will not change the current working tree,
    immediately.
    """

    def __init__(self, framework, basename):
        assert framework.options.has_key('stdconfig')
        configuration = ".%s" % framework.options['stdconfig']
        LinuxConfiguration.__init__(self, framework,
                                    basename=basename, nth=configuration)

        self.cppflags = '/dev/null'
        self.source   = basename
        self.kconfig  = '.config.allyesconfig'

    def expand(self, verify=False):
        return self.expand_stdconfig()

    def get_cppflags(self):
        return ""

    def filename(self):
        return self.basename + '.' + self.framework.options['stdconfig']

    def __repr__(self):
        # This may look like a proper filename, but actually is fake
        return '%s.%s' % (self.source, self.framework.options['stdconfig'])


class BusyboxConfiguration(KbuildConfiguration):
    """
    This class represents a (partial) Busybox configuration.

    The expand method uses Kconfig to fill up the remaining
    variables. The parameter expansion_strategy of the __init__ method
    selects how partial configurations get expanded. In the default
    mode, 'defconfig', the strategy is to set the remaining variables
    to their Kconfig defined defaults. With 'allnoconfig', the strategy
    is to enable as few features as possible.

    The attribute 'model' of this class is allocated in the expand()
    method on demand.

    """
    def __init__(self, framework, basename, nth):
        KbuildConfiguration.__init__(self, framework, basename, nth)
        self.arch = 'busybox'
        self.subarch = 'busybox'

        if self.framework.options.has_key('expansion_strategy'):
            self.expansion_strategy = self.framework.options['expansion_strategy']
        else:
            self.expansion_strategy = 'allyesconfig'

    def __repr__(self):
        return '<BusyboxConfiguration "' + self.kconfig + '">'


class BusyboxPartialConfiguration(BusyboxConfiguration):
    """
    This class creates a configuration object for a partial Busybox
    Configuration. This works on arbitrary partial configurations, like
    "trolled" ones.

    NB: the self.cppflags and self.source is set to "/dev/null"
    """

    def __init__(self, framework, filename):
        BusyboxConfiguration.__init__(self, framework, basename=filename, nth="")

        self.cppflags = '/dev/null'
        self.source   = '/dev/null'
        self.kconfig  = filename

    def write_config_h(self, dummy):
        pass

    def filename(self):
        return self.basename


class BusyboxStdConfiguration(BusyboxConfiguration):
    """
    This class creates a configuration object for a standard Busybox
    configuration, such as 'allyesconfig' or 'allnoconfig'.

    Instantiating this class will not change the current working tree,
    immediately.
    """

    def __init__(self, framework, basename):
        assert framework.options.has_key('stdconfig')
        configuration = ".%s" % framework.options['stdconfig']
        BusyboxConfiguration.__init__(self, framework,
                                    basename=basename, nth=configuration)

        self.cppflags = '/dev/null'
        self.source   = basename
        self.kconfig  = '.config.allyesconfig'

    def expand(self, verify=False):
        return self.expand_stdconfig()

    def get_cppflags(self):
        return ""

    def filename(self):
        return self.basename + '.' + self.framework.options['stdconfig']

    def __repr__(self):
        # This may look like a proper filename, but actually is fake
        return '%s.%s' % (self.source, self.framework.options['stdconfig'])


class CorebootConfiguration(KbuildConfiguration):
    """
    This class represents a (partial) Coreboot configuration.

    The expand method uses Kconfig to fill up the remaining
    variables. The framework parameter of the __init__ method
    selects how partial configurations get expanded. In the default
    mode, 'allyesconfig', the strategy is to set all valid remaining variables
    to yes. With 'allnoconfig', the strategy is to enable as few features as
    possible.

    The attribute 'model' of this class is allocated in the expand()
    method on demand.

    """
    def __init__(self, framework, basename, nth):
        KbuildConfiguration.__init__(self, framework, basename, nth)
        self.arch = 'coreboot'
        if os.environ.has_key('SUBARCH'):
            self.subarch = os.environ['SUBARCH']
        else:
            self.subarch = "emulation/qemu-x86"

        if self.framework.options.has_key('expansion_strategy'):
            self.expansion_strategy = self.framework.options['expansion_strategy']
        else:
            self.expansion_strategy = 'allyesconfig'

    def __repr__(self):
        return '<CorebootConfiguration "' + self.kconfig + '">'

    def call_gcc(self, on_file):
        raise CheckerNotImplemented("call_gcc is not implemented yet")

    def call_spatch(self, on_file):
        raise CheckerNotImplemented("call_spatch is not implemented yet")

    def call_sparse(self, on_file):
        raise CheckerNotImplemented("call_sparse is not implemented yet")


class CorebootPartialConfiguration(CorebootConfiguration):
    """
    This class creates a configuration object for a partial Coreboot
    Configuration. This works on arbitrary partial configurations, like
    "trolled" ones.

    NB: the self.cppflags and self.source is set to "/dev/null"
    """

    def __init__(self, framework, filename):
        CorebootConfiguration.__init__(self, framework, basename=filename, nth="")

        self.cppflags = '/dev/null'
        self.source   = '/dev/null'
        self.kconfig  = filename

    def write_config_h(self, dummy):
        pass

    def filename(self):
        return self.basename


class CorebootStdConfiguration(CorebootConfiguration):
    """
    This class creates a configuration object for a standard Coreboot
    configuration, such as 'allyesconfig' or 'allnoconfig'.

    Instantiating this class will not change the current working tree,
    immediately.
    """

    def __init__(self, framework, basename):
        assert framework.options.has_key('stdconfig')
        configuration = ".%s" % framework.options['stdconfig']
        CorebootConfiguration.__init__(self, framework,
                                    basename=basename, nth=configuration)

        self.cppflags = '/dev/null'
        self.source   = basename
        self.kconfig  = '.config.allyesconfig'

    def expand(self, verify=False):
        return self.expand_stdconfig()

    def get_cppflags(self):
        return ""

    def filename(self):
        return self.basename + '.' + self.framework.options['stdconfig']

    def __repr__(self):
        # This may look like a proper filename, but actually is fake
        return '%s.%s' % (self.source, self.framework.options['stdconfig'])


"""vamos - fiasco interfacing"""

# Copyright (C) 2012 Christian Dietrich <christian.dietrich@informatik.uni-erlangen.de>
# Copyright (C) 2014 Stefan Hengelein <stefan.hengelein@fau.de>
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
#

import vamos.golem.kbuild as kbuild
import vamos.tools as tools
import vamos.model as Model

from tempfile import NamedTemporaryFile

import glob
import logging
import os
import re
import sys


class InferenceAtoms:
    """ Baseclass for project related information to create inferences """
    def __init__(self):
        pass
    def OP_list(self, selection):
        raise NotImplementedError
    def OP_features_in_pov(self, selection):
        raise NotImplementedError

    def OP_domain_of_variability_intention(self, var_int):
        """Give back what the domain a variability intention can have (a set)"""
        # pylint: disable=W0613
        return set(["n", "m", "y"])

    def OP_default_value_of_variability_intention(self, var_int):
        """The value a variability intention has, when it is undefined"""
        # pylint: disable=W0613
        return "n"

    def format_var_impl(self, var_impl):
        return var_impl

    def format_selections(self, selections):
        if len(selections) == 0:
            return ""
        if len(selections) == 1:
            return str(selections[0])
        return "((" + ") || (".join([str(x) for x in selections]) + "))"

    def pov_worth_working_on(self, point_of_variability):
        # pylint: disable=W0613
        return True

class FiascoInferenceAtoms(InferenceAtoms):
    """ Project specific information to create inferences for Fiasco """
    BSP_dict = {"arm": ["imx", "integrator", "kirkwood", "omap3", "pxa", "realview", "s3c", "sa1100",
                             "tegra2"],
                     "ppc32": ["mpc52xx", "qemu"]}
    def __init__(self):
        InferenceAtoms.__init__(self)

    def OP_list(self, selection):
        """
        to be run in a fiasco source tree

        The parameter features represents a (partial) config selection in
        the form of a dict from feature -> value e.g: {'CONFIG_X86': 'y',
        'CONFIG_BARFOO': 'm'}.

        @return a tuple of ([variability_implementation],
                            [point_of_variability]).
        """

        features = selection.to_dict()

        arch = features.get("CONFIG_XARCH", None)
        if arch in ["arm", "ppc32"]:
            bsp = features.get("CONFIG_BSP_NAME", None)
            if not bsp in self.BSP_dict[arch]:
                return (set(), set(["src/Modules.%s" % arch]))

        if features.get("CONFIG_MP", None) != None:
            features["MPCORE_PHYS_BASE"] = "23"

        scriptsdir = kbuild.find_scripts_basedir()
        assert(os.path.exists(os.path.join(scriptsdir, 'Makefile.list_fiasco')))

        fd = NamedTemporaryFile()
        logging.debug("dumping partial configuration with %d items to %s", len(features.items()), fd.name)
        for (key, value) in features.items():
            fd.write("%s=%s\n" % (key, value))
            logging.debug("%s=%s", key, value)
        fd.flush()

        make="make -f %(basedir)s/Makefile.list_fiasco auto_conf=%(tempfile)s" % \
            { 'basedir' : scriptsdir,
              'tempfile': fd.name}

        (stdout, ret) = tools.execute(make)
        assert ret == 0
        stdout = {tuple((x + " ").split(" ", 1)) for x in stdout}
        if not stdout.has_key("MAKEFILE_LIST"):
            raise tools.CommandFailed("Makefile.list_fiasco", -1, stdout)
        if not stdout.has_key("PREPROCESS_PARTS"):
            raise tools.CommandFailed("Makefile.list_fiasco", -1, stdout)

        var_impl = {x for x in stdout["PREPROCESS_PARTS"].split() if len(x) > 0}
        var_points = {x for x in stdout["MAKEFILE_LIST"].split() if len(x) > 0 and x != fd.name}
        return (var_impl, var_points)

    def OP_features_in_pov(self, point_of_variability):
        ret = set()
        bsp = False
        with open(point_of_variability) as fd:
            for line in fd:
                for m in re.finditer(r'CONFIG_(?P<feature>[A-Za-z0-9_]+)', line):
                    config_variable = m.group('feature')
                    if config_variable == "BSP_NAME":
                        bsp = True
                    else:
                        ret.add("CONFIG_" + config_variable)
        if bsp:
            return [["CONFIG_BSP"]] + [["CONFIG_BSP_NAME", x] for x in ret]
        else:
            return [[x] for x in ret]

    def OP_domain_of_variability_intention(self, var_int):
        if "CONFIG_XARCH" == var_int:
            return set([x[len("src/Modules."):] for x in glob.glob("src/Modules.*")
                    if not x.endswith("generic")])
        if "CONFIG_BSP_NAME" == var_int:
            return set(reduce(lambda x,y:x+y, self.BSP_dict.values()))
        if "CONFIG_ABI" == var_int:
            return set(["vf"])
        return set(["n", "y"])

    def format_var_impl(self, var_impl):
        return "HOMUTH_" + kbuild.normalize_filename(var_impl)

    def format_selections(self, selections):
        replacements = {"=y": "",
                        "CONFIG_XARCH=arm": "CONFIG_ARM",
                        "CONFIG_XARCH=ux": "CONFIG_PF_UX",
                        "CONFIG_XARCH=amd64": "(CONFIG_AMD64 && CONFIG_PF_PC)",
                        "CONFIG_XARCH=ia32": "(CONFIG_IA32 && CONFIG_PF_PC)",
                        "CONFIG_XARCH=ppc": "CONFIG_PPC",
                        "CONFIG_BSP_NAME=realview": "CONFIG_PF_REALVIEW",
                        "CONFIG_BSP_NAME=imx": "CONFIG_PF_IMX",
                        "CONFIG_BSP_NAME=pxa": "CONFIG_PF_XSCALE",
                        "CONFIG_BSP_NAME=s3c": "CONFIG_PF_S3C2410",
                        "CONFIG_BSP_NAME=tegra2": "CONFIG_PF_TEGRA2",
                        "CONFIG_BSP_NAME=mpc52xx": "CONFIG_PF_MPC52XX",
                        "CONFIG_BSP_NAME=omap": "CONFIG_PF_OMAP",
                        "CONFIG_BSP_NAME=sa1100": "CONFIG_PF_SA1100",
                        "CONFIG_BSP_NAME=kirkwood": "CONFIG_PF_KIRKWOOD",
                        "CONFIG_BSP_NAME=integrator": "CONFIG_PF_INTEGRATOR",
                        "CONFIG_BSP_NAME=qemu": "CONFIG_PF_QEMU",
                        "CONFIG_ABI=vf": "CONFIG_ABI_VF",
                        }

        string = InferenceAtoms.format_selections(self, selections)
        for (k,v) in replacements.items():
            string = string.replace(k,v)

        return string


class LinuxInferenceAtoms(InferenceAtoms):
    """ Project specific information to create inferences for Linux """
    def __init__(self, arch, subarch, directory_prefix = ""):
        InferenceAtoms.__init__(self)
        self.arch = arch
        self.subarch = subarch
        self.directory_prefix = directory_prefix

        if arch:
            modelfile = Model.get_model_for_arch(arch)
        else:
            modelfile = None

        if modelfile:
            logging.info("loading model %s", modelfile)
            self.model = Model.parse_model(modelfile)
        else:
            sys.exit("No model for '%s' found, please generate models using undertaker-kconfigdump" \
                    % arch)

        kbuild.call_linux_makefile('allnoconfig', arch=self.arch, subarch=self.subarch)
        kbuild.apply_configuration(arch=self.arch, subarch=self.subarch)

    def OP_list(self, selection):
        features = selection.to_dict()
        (files, dirs) = kbuild.files_for_selected_features(features, self.arch, self.subarch)
        return (files, dirs)

    def OP_features_in_pov(self, point_of_variability):
        variables = kbuild.determine_buildsystem_variables_in_directory(point_of_variability)
        return [[x] for x in variables]

    def OP_default_value_of_variability_intention(self, var_int):
        return "n"

    def OP_domain_of_variability_intention(self, var_int):
        if var_int + "_MODULE" in self.model:
            return set(["y", "n", "m"])
        return set(["n", "y"])

    def pov_worth_working_on(self, point_of_variability):
        if not point_of_variability.startswith(self.directory_prefix):
            logging.info("Skipping %s, not in scope", point_of_variability)
            return False
        return True

    def format_var_impl(self, var_impl):
        sourcefile = kbuild.guess_source_for_target(var_impl, self.arch)
        if sourcefile:
            var_impl = kbuild.normalize_filename(sourcefile)
        else:
            logging.warning("Failed to guess source file for %s", var_impl)
        return "FILE_" + var_impl

    def format_selections(self, selections):
        string = InferenceAtoms.format_selections(self, selections)
        string = string.replace("=y", "")
        string = string.replace("=m", "_MODULE")
        return string


class BusyboxInferenceAtoms(LinuxInferenceAtoms):
    """ Project specific  information to create inferences for Busybox """
    def __init__(self, directory_prefix = ""):
        LinuxInferenceAtoms.__init__(self, "busybox", None)
        self.directory_prefix = directory_prefix
        tools.execute("make gen_build_files", failok=False)

    def OP_domain_of_variability_intention(self, var_int):
        return set(["n", "y"])

class CorebootInferenceAtoms(LinuxInferenceAtoms):
    """ Project specific information to create inferences for Coreboot """
    def __init__(self, path):
        LinuxInferenceAtoms.__init__(self, "coreboot", None)
        self.directory_prefix = path

    def OP_list(self, selection):
        features = selection.to_dict()
        (files, dirs) = kbuild.files_for_selected_features(features, 'coreboot')
        if len(selection) == 0:
            dirs.add("Makefile.inc")
        return files, dirs

    def OP_features_in_pov(self, point_of_variability):
        ret = set()
        mainboarddir = False
        with open(point_of_variability) as fd:
            for line in fd:
                if "MAINBOARDDIR" in line:
                    mainboarddir = True
                for m in re.finditer(r'CONFIG_(?P<feature>[A-Za-z0-9_]+)', line):
                    config_variable = m.group('feature')
                    ret.add("CONFIG_" + config_variable)
        features = [[x] for x in ret]
        if mainboarddir:
            features += [["CONFIG_MAINBOARD_DIR", x] for x in ret
                         if x !=  "CONFIG_MAINBOARD_DIR"]
        return features

    def OP_domain_of_variability_intention(self, var_int):
        """Give back what the domain a variability intention can have (a set)"""
        # pylint: disable=W0613
        if var_int == "CONFIG_MAINBOARD_DIR":
            possible_values = set()
            vendors = os.listdir("src/mainboard")
            for vendor in vendors:
                vendor_path = os.path.join("src/mainboard", vendor)
                if not os.path.isdir(vendor_path):
                    continue
                for board in os.listdir(vendor_path):
                    if not os.path.isdir(os.path.join(vendor_path, board)):
                        continue
                    possible_values.add("%s/%s" %(vendor, board))
            return possible_values
        return set(["n", "y"])

    def format_selections(self, selections):
        string = InferenceAtoms.format_selections(self, selections)
        string = string.replace("=y", "")
        string = string.replace("-", "_")
        def MAINBOARD_DIR(match):
            return "(CONFIG_VENDOR_%(vendor)s && CONFIG_BOARD_%(vendor)s_%(board)s)" % { \
                'vendor': match.group(1).upper(),
                'board': match.group(2).upper()}

        string = re.sub('CONFIG_MAINBOARD_DIR=([^)&|>< -]*)/([^)&|<> -]*)',
                        MAINBOARD_DIR, string)
        return string

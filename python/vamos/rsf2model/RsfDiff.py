
""" Utilities to diff RSF models generated by undertaker-kconfigdump. """

# Copyright (C) 2015 Valentin Rothberg <rothberg@cs.fau.de>

# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.

import logging as log
import re
import subprocess

from vamos.rsf2model import tools as rsftools


def logically_equivalent(formula_a, formula_b):
    """ Return True if both formulas are logically equivalent, otherwise return
    False. """
    if not formula_a and not formula_b:
        # If both formulas are empty return True
        return True
    if not formula_a or not formula_b:
        # If one formula is empty return False
        return False

    # Substitute all "CHOICE_*" symbols with "__CHOICE"
    regex = re.compile(r"\bCHOICE_[\d]+\b")
    formula_a = regex.sub("__CHOICE", formula_a)
    formula_b = regex.sub("__CHOICE", formula_b)

    # Turn formulas into limboole-conform input
    formula_a = formula_a.replace('&&', '&').replace('||', '|')
    formula_b = formula_b.replace('&&', '&').replace('||', '|')
    formula_a = formula_a.replace('!=', '& !').replace('=', '&')
    formula_b = formula_b.replace('!=', '& !').replace('=', '&')

    # Bi-implication to check logical equivalence
    biimpl = formula_a + " <-> " + formula_b

    # Check logical equivalence with limboole
    proc = subprocess.Popen("limboole", stdout=subprocess.PIPE,
                            stdin=subprocess.PIPE, shell=True)
    valid = proc.communicate(input=biimpl)[0].split()[1]

    if valid == "VALID":
        return True

    return False


def get_symbols(formula):
    """ Return a set of symbols mentioned in @formula.  "CHOICE_*" symbols are
    ignored. """
    symbols = re.findall(r"(?:\w*[A-Z0-9]\w*){2,}", formula)
    symbols = [x for x in symbols if not x.startswith("CHOICE_")]
    return set(symbols)


class RsfDiff(object):
    """ Diff two RSF models. """

    def __init__(self, rsf_a, rsf_b, debug=False):
        """ Diff @rsf_a and @rsf_b.  Set @debug to True to get (verbose) logging
        output. """
        self.rsf_a = rsf_a
        self.rsf_b = rsf_b
        self.changes = {}  # dictionary to store diff information

        if debug:
            log.basicConfig(level=log.DEBUG)

    def diff(self):
        """ Diff the models. """
        # First collect a set of symbols of both rsf files
        symbols_a = self.rsf_a.database.get('Item')
        symbols_a.extend(self.rsf_a.database.get('ChoiceItem'))
        symbols_a = set([x[0] for x in symbols_a])

        symbols_b = self.rsf_b.database.get('Item')
        symbols_b.extend(self.rsf_b.database.get('ChoiceItem'))
        symbols_b = set([x[0] for x in symbols_b])

        all_symbols = sorted(symbols_a | symbols_b)

        # Diff both rsf models symbol-wise
        for symbol in all_symbols:
            in_a, in_b = False, False
            if symbol in symbols_a:
                in_a = True
            if symbol in symbols_b:
                in_b = True

            # Symbol removed?
            if in_a and not in_b:
                log.debug("Symbol removed: %s", symbol)
                self.changes["REM_" + symbol] = None
                continue
            # Symbol added?
            elif not in_a and in_b:
                log.debug("Symbol added: %s", symbol)
                self.changes["ADD_" + symbol] = None
                continue

            self.changes["MOD_" + symbol] = {}

            # Attribute changed?
            self.diff_attribute(symbol)

            # Dependency changed?
            self.diff_depends(symbol)

            # Default changed?
            self.diff_defaults(symbol)

            # Select changed?
            self.diff_selects(symbol)

            # Delete the "MOD_*" entry if nothing has been modified
            if not self.changes["MOD_" + symbol]:
                del self.changes["MOD_" + symbol]

    @rsftools.memoized
    def get_added_features(self):
        """ Return added features of the current diff.  Must be called after
        RsfDiff.diff() """
        return sorted([x[4:] for x in self.changes.keys() if x.startswith("ADD_")])

    @rsftools.memoized
    def get_removed_features(self):
        """ Return removed features of the current diff.  Must be called after
        RsfDiff.diff() """
        return sorted([x[4:] for x in self.changes.keys() if x.startswith("REM_")])

    @rsftools.memoized
    def get_modified_features(self):
        """ Return modified features of the current diff. Must be called after
        RsfDiff.diff() """
        return sorted([x[4:] for x in self.changes.keys() if x.startswith("MOD_")])

    @rsftools.memoized
    def get_changed_features(self):
        """ Return all changed features (i.e., added, removed, modified) of the
        current diff.  Must be called after RsfDiff.diff() """
        return sorted(self.get_added_features() +   \
                      self.get_removed_features() + \
                      self.get_modified_features())

    def find_relevant_diffs(self, symbols, model):
        """ Return a dict of changes that affect items in @symbols.  The dict
        has the format {symbol: key to self.changes}, so that the actual changes
        can be queried from self.changes afterwards.  The @symbols will be
        sliced in @model to find all relevant diffs. """
        changes = {}
        slices = model.slice_symbols(symbols)
        for symbol in slices:
            for key in self.changes.keys():
                if key[4:] == symbol:
                    # Only add the key of changes to avoid duplications
                    changes[symbol] = key
                    break
        return changes

    def diff_attribute(self, symbol):
        """ Diff attribute of @symbol.  An attribute (i.e., type + prompt)
        cannot be added or removed since Kconfig is typed.  Since MOD_TYPE and
        {ADD,REM}_PROMPT indicate that an attribute has been modified, we do not
        use an explicit MOD_ATTRIBUTE entry. """
        mod = {}

        # Type changed?
        type_a = self.rsf_a.get_type(symbol)
        type_b = self.rsf_b.get_type(symbol)
        if type_a != type_b:
            log.debug("Type of %s changed: %s => %s", symbol, type_a, type_b)
            mod["MOD_TYPE"] = [type_a, type_b]

        # Prompt changed?
        prompts_a = self.rsf_a.get_prompts(symbol)
        prompts_b = self.rsf_b.get_prompts(symbol)
        if prompts_a != prompts_b:
            if int(prompts_a) < int(prompts_b):
                log.debug("Prompt added to %s", symbol)
                mod["ADD_PROMPT"] = [prompts_a, prompts_b]
            else:
                log.debug("Prompt removed from %s", symbol)
                mod["REM_PROMPT"] = [prompts_a, prompts_b]

        # Update diff dictionary if something changed
        if mod:
            self.changes["MOD_" + symbol].update(mod)

    def diff_depends(self, symbol):
        """ Diff dependencies of @symbol. """
        # pylint: disable=R0914
        mod = {}

        expr_a = self.rsf_a.get_depends(symbol)
        expr_b = self.rsf_b.get_depends(symbol)
        differ = False

        if not expr_a and not expr_b:
            return

        if expr_a and not expr_b:
            log.debug("Dependencies removed from %s", symbol)
            differ = True
            mod["REM_DEPENDS"] = [expr_a]
        elif not expr_a and expr_b:
            log.debug("Dependencies added to %s", symbol)
            differ = True
            mod["ADD_DEPENDS"] = [expr_b]

        if differ:
            self.changes["MOD_" + symbol].update(mod)
            return

        differ = False
        mod["MOD_DEPENDS"] = {}
        # References changed?
        refs_a = get_symbols(expr_a)
        refs_b = get_symbols(expr_b)
        if refs_a != refs_b:
            log.debug("Dependency references of %s changed:", symbol)
            differ = True
            added = refs_b - refs_a
            removed = refs_a - refs_b
            if added:
                log.debug("\tAdded  : %s", ", ".join(added))
                mod["MOD_DEPENDS"]["ADD_REFERENCES"] = sorted(added)
            if removed:
                log.debug("\tRemoved: %s", ", ".join(removed))
                mod["MOD_DEPENDS"]["REM_REFERENCES"] = sorted(removed)

        # Expression changed?
        #
        # Note that we do not check for logic equivalence if the references
        # changed!
        if not differ and not logically_equivalent(expr_a, expr_b):
            log.debug("Dependency expression of %s changed:", symbol)
            log.debug("\tOld condition: %s", expr_a)
            log.debug("\tNew condition: %s", expr_b)
            mod["MOD_DEPENDS"]["MOD_EXPRESSION"] = [expr_a, expr_b]

        # Update diff dictionary if something has changed
        if not mod["MOD_DEPENDS"]:
            del mod["MOD_DEPENDS"]
        if mod:
            self.changes["MOD_" + symbol].update(mod)

    def diff_selects(self, symbol):
        """ Diff selects of @symbol. """
        # pylint: disable=R0914
        mod = {}
        mod["MOD_SELECTS"] = {}

        selects_a = self.rsf_a.get_selects(symbol)
        selects_b = self.rsf_b.get_selects(symbol)

        if not selects_a and not selects_b:
            return False

        # Targets changed?
        #
        # Note that a select is identified by its target.  If one target is
        # selected multiple times, the conditions are concatenated (OR-ed).
        targets_a = set([x[0] for x in selects_a])
        targets_b = set([x[0] for x in selects_b])
        targets = targets_a & targets_b
        if targets_a != targets_b:
            log.debug("Selects (targets) of %s changed:", symbol)
            added = targets_b - targets_a
            removed = targets_a - targets_b
            if added:
                log.debug("\tAdded  : %s", ", ".join(added))
                mod["ADD_SELECTS"] = sorted(added)
            if removed:
                log.debug("\tRemoved: %s", ", ".join(removed))
                mod["REM_SELECTS"] = sorted(removed)

        for target in targets:
            # Multiple selects on the same target are possible, so concatenate
            # the conditions
            cond_a = [x[1] for x in selects_a if x[0] == target]
            cond_b = [x[1] for x in selects_b if x[0] == target]
            cond_a = "(" + ") || (".join(cond_a) + ")"
            cond_b = "(" + ") || (".join(cond_b) + ")"

            # References of select-target changed?
            refs_a = get_symbols(cond_a)
            refs_b = get_symbols(cond_b)

            if refs_a != refs_b:
                log.debug("Select references of %s->%s changed:",
                          symbol, target)
                added = refs_b - refs_a
                removed = refs_a - refs_b
                if added:
                    log.debug("\tAdded  : %s", ", ".join(added))
                    mod["MOD_SELECTS"]["ADD_REFERENCES_" + target] = sorted(added)
                if removed:
                    log.debug("\tRemoved: %s", ", ".join(removed))
                    mod["MOD_SELECTS"]["REM_REFERENCES_" + target] = sorted(removed)

                # Do not check logical equivalence if references differ already
                continue

            # Condition changed?
            if not logically_equivalent(cond_a, cond_b):
                log.debug("Select condition of %s->%s changed:", symbol, target)
                log.debug("\tOld condition: %s", cond_a)
                log.debug("\tNew condition: %s", cond_b)
                mod["MOD_SELECTS"]["MOD_CONDITION_" + target] = [cond_a, cond_b]

        # Update diff dictionary if something has changed
        if not mod["MOD_SELECTS"]:
            del mod["MOD_SELECTS"]
        if mod:
            self.changes["MOD_" + symbol].update(mod)

    def diff_defaults(self, symbol):
        """ Diff defaults of @symbol. """
        # pylint: disable=R0914
        mod = {}

        defaults_a = self.rsf_a.get_defaults(symbol)
        defaults_b = self.rsf_b.get_defaults(symbol)

        if not defaults_a and not defaults_b:
            return

        mod["MOD_DEFAULTS"] = {}
        # Default added or removed?
        values_a = set(x[0] for x in defaults_a)
        values_b = set(x[0] for x in defaults_b)
        values = values_a & values_b
        if values_a != values_b:
            log.debug("Default value(s) of %s changed:", symbol)
            added = values_b - values_a
            removed = values_a - values_b
            if added:
                log.debug("\tAdded  : %s", ", ".join(added))
                mod["ADD_DEFAULTS"] = sorted(added)
            if removed:
                log.debug("\tRemoved: %s", ", ".join(removed))
                mod["REM_DEFAULTS"] = sorted(removed)

        # Conditions changed?
        for value in values:
            # Multiple conditions for the same value are possible, so
            # concatenate the conditions
            cond_a = [x[1] for x in defaults_a if x[0] == value]
            cond_b = [x[1] for x in defaults_b if x[0] == value]
            cond_a = "(" + ") || (".join(cond_a) + ")"
            cond_b = "(" + ") || (".join(cond_b) + ")"

            # References of select-target changed?
            refs_a = get_symbols(cond_a)
            refs_b = get_symbols(cond_b)

            if refs_a != refs_b:
                log.debug("Default references of %s->%s changed:",
                          symbol, value)
                added = refs_b - refs_a
                removed = refs_a - refs_b
                if added:
                    log.debug("\tAdded  : %s", ", ".join(added))
                    mod["MOD_DEFAULTS"]["ADD_REFERENCES_" + value] = sorted(added)
                if removed:
                    log.debug("\tRemoved: %s", ", ".join(removed))
                    mod["MOD_DEFAULTS"]["REM_REFERENCES_" + value] = sorted(removed)

                # Do not check logical equivalence if references differ already
                continue

            # Condition changed?
            if not logically_equivalent(cond_a, cond_b):
                log.debug("Default condition of %s->%s changed:", symbol, value)
                log.debug("\tOld condition: %s", cond_a)
                log.debug("\tNew condition: %s", cond_b)
                mod["MOD_DEFAULTS"]["MOD_CONDITION_" + value] = [cond_a, cond_b]

        # Update diff dictionary if something has changed
        if not mod["MOD_DEFAULTS"]:
            del mod["MOD_DEFAULTS"]
        if mod:
            self.changes["MOD_" + symbol].update(mod)

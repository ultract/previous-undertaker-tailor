/*
 *   undertaker - analyze preprocessor blocks in code
 *
 * Copyright (C) 2011 Christian Dietrich <christian.dietrich@informatik.uni-erlangen.de>
 * Copyright (C) 2012 Bernhard Heinloth <bernhard@heinloth.net>
 * Copyright (C) 2012 Valentin Rothberg <valentinrothberg@gmail.com>
 * Copyright (C) 2012 Andreas Ruprecht  <rupran@einserver.de>
 * Copyright (C) 2013-2014 Stefan Hengelein <stefan.hengelein@fau.de>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */


#include "PumaConditionalBlock.h"
#include "Logging.h"
#include "cpp14.h"

#include <Puma/CTranslationUnit.h>
#include <Puma/CUnit.h>
#include <Puma/UnitManager.h>
#include <Puma/ManipCommander.h>
#include <Puma/Token.h>
#include <Puma/TokenStream.h>
#include <Puma/PreTreeNodes.h>
#include <Puma/PreprocessorParser.h>
#include <Puma/PreFileIncluder.h>
#include <Puma/PreSonIterator.h>
#include <Puma/StrCol.h>

#include <set>

using namespace Puma;


/************************************************************************/
/* static functions */
/************************************************************************/

// Build a string from a subtree of the preprocessor syntax tree.
static char* buildString (const PreTree* node) {
    assert(node);

    char *result, *ptr;
    std::string str;

    // If subtree isn't empty concatenate all tokens to a single string.
    if (node->sons ()) {
        PreTokenListPart* list = (PreTokenListPart*) node->son (0);

        // Fill the buffer.
        for (unsigned int i = 0; i < (unsigned int) list->sons (); i++) {
            str += ((PreTreeToken*) list->son (i))->token ()->text ();
        }
    }

    // eat trailing whitespace
    std::string::size_type i = str.length();
    while (str[i-1] == ' ' || str[i-1] == '\t')
        --i;
    str.erase(i);

    // Create the return string buffer.
    ptr = result = new char[str.size() + 5];

    // Copy the buffer into the return buffer but skip newlines.
    for (char & elem : str)
        if (elem != '\n')
            *ptr++ = elem;

    // Finish return buffer.
    *ptr = '\0';

    assert(result);
    return result;
}

/************************************************************************/
/* PumaConditionalBlock                                                 */
/************************************************************************/

const char * PumaConditionalBlock::ExpressionStr() const {
    assert(_parent);
    const PreTree *node;

    assert(_current_node);

    if (_expressionStr_cache)
      return _expressionStr_cache;

    if ((node = dynamic_cast<const PreIfDirective *>(_current_node))) {
        _expressionStr_cache = buildString(node->son(1));
    } else if ((node = dynamic_cast<const PreIfdefDirective *>(_current_node))) {
        _expressionStr_cache = Puma::StrCol::dup(node->son(1)->startToken()->text());
    } else if ((node = dynamic_cast<const PreIfndefDirective *>(_current_node))) {
        _expressionStr_cache = Puma::StrCol::dup(node->son(1)->startToken()->text());
    } else if ((node = dynamic_cast<const PreElifDirective *>(_current_node))) {
        _expressionStr_cache = buildString(node->son(1));
    } else if (isElseBlock()) {
        _expressionStr_cache = Puma::StrCol::dup((char *)"");
        return _expressionStr_cache;
    }

    if (_expressionStr_cache) {
        PreMacroExpander expander(_builder.cpp_parser());
        char *tmp = _expressionStr_cache;
        _expressionStr_cache = expander.expandMacros(_expressionStr_cache);
        // expandMacros allocates new memory, so we have to cleanup the old memory
        delete [] tmp;
        return _expressionStr_cache;
    } else {
        return "??";
    }
}

const std::string PumaConditionalBlock::getName() const {
    if (!_parent) {
        return "B00"; // top level block, represents file
    } else {
        std::string s("B");
        s += std::to_string(_number);
        if (useBlockWithFilename)
            // get the normalized file variable without "FILE" prefix and append to the block name
            s += &fileVar()[4];
        return s;
    }
}

bool PumaConditionalBlock::isIfndefine() const {
    return dynamic_cast<const PreIfndefDirective *>(_current_node) != nullptr;
}

bool PumaConditionalBlock::isElseIfBlock() const {
    return dynamic_cast<const PreElifDirective *>(_current_node) != nullptr;
}

bool PumaConditionalBlock::isElseBlock() const {
    return dynamic_cast<const PreElseDirective *>(_current_node) != nullptr;
}

/************************************************************************/
/* PumaConditionalBlockBuilder                                          */
/************************************************************************/

void PumaConditionalBlockBuilder::iterateNodes (PreTree *node) {
    PreSonIterator i(node);

    for (i.first(); !i.isDone(); i.next())
        i.currentItem()->accept(*this);
}

void undertaker_normalizations(Puma::Unit *);

ConditionalBlock *PumaConditionalBlockBuilder::parse(const std::string &filename) {
    _project = make_unique<Puma::CProject>(_err, nullptr, nullptr);
    _unit = _project->scanFile(filename.c_str());
    if (!_unit) {
        Logging::error("Failed to parse: ", filename);
        return nullptr;
    }

    // do some normalizations
    undertaker_normalizations(_unit);

    _tu = make_unique<CTranslationUnit>(*_unit, *_project);

    // prepare C preprocessor
    TokenStream stream;           // linearize tokens from several files
    stream.push (_unit);
    _project->unitManager().init();

    _cpp = make_unique<PreprocessorParser>(&_err, &_project->unitManager(),
            &_tu->local_units(), std::cerr);
    _cpp->macroManager()->init(_unit->name());
    _cpp->stream (&stream);
    _cpp->configure (_project->config ());

    /* Resolve all #include statements, must be done after _cpp initialization */
    resolve_includes(_unit);
    stream.reset();
    stream.push(_unit);

    _cpp->silentMode ();
    _cpp->parse ();
    /* After parsing we have to reset the macro manager */
    reset_MacroManager(_unit);

    Puma::PreTree *ptree = _cpp->syntaxTree();
    if (!ptree) {
        Logging::error("Failed to create cpp tree from file : ", filename);
        return nullptr;
    }
    ptree->accept(*this);
    return _current;
}

#if 0
#define TRACECALL                                                                                 \
    Logging::error(__PRETTY_FUNCTION__, ": ", "Start: ", node->startToken()->location().line(),   \
                   ", ", "End: ", node->endToken()->location().line());
#else
#define TRACECALL
#endif

#ifndef __unused
#define __unused __attribute__((unused))
#endif

void PumaConditionalBlockBuilder::visitPreProgram_Pre (PreProgram *node) {
    if (node->startToken()) {
        TRACECALL;
    }

    assert (!_current);
    assert (_unit);

    _nodeNum = 0;
    _current = new PumaConditionalBlock(_file, nullptr, nullptr, node, 0, *this);
    _current->_isIfBlock = true;
    _current->_start = node->startToken();
    _current->_end   = node->endToken();
    _condBlockStack.push(_current);
}

void PumaConditionalBlockBuilder::visitPreProgram_Post (__unused PreProgram *node ) {
    if (node->startToken()) {
        TRACECALL;
    }
    _condBlockStack.pop();
}

void PumaConditionalBlockBuilder::visitPreIfDirective_Pre (PreIfDirective *node) {
    TRACECALL;

    PumaConditionalBlock *parent = _condBlockStack.top();
    _current = new PumaConditionalBlock(_file, parent, nullptr, node, _nodeNum++, *this);
    _current->_start = node->startToken();
    _condBlockStack.push(_current);
    _current->_isIfBlock = true;
    _file->push_back(_condBlockStack.top());
    parent->push_back(_current);
}

void PumaConditionalBlockBuilder::visitPreIfdefDirective_Pre (PreIfdefDirective *node) {
    TRACECALL;

    PumaConditionalBlock *parent = _condBlockStack.top();
    _current = new PumaConditionalBlock(_file, parent, nullptr, node, _nodeNum++, *this);
    _current->_start = node->startToken();
    _condBlockStack.push(_current);
    _current->_isIfBlock = true;
    _file->push_back(_condBlockStack.top());
    parent->push_back(_current);
}

void PumaConditionalBlockBuilder::visitPreIfndefDirective_Pre (PreIfndefDirective *node) {
    TRACECALL;

    PumaConditionalBlock *parent = _condBlockStack.top();
    _current = new PumaConditionalBlock(_file, parent, nullptr, node, _nodeNum++, *this);
    _current->_start = node->startToken();
    _condBlockStack.push(_current);
    _current->_isIfBlock = true;
    _file->push_back(_condBlockStack.top());
    parent->push_back(_current);
}

void PumaConditionalBlockBuilder::visitPreElifDirective_Pre (PreElifDirective *node) {
    TRACECALL;

    assert(_current);
    assert(_file);

    PumaConditionalBlock *prev = _condBlockStack.top();
    _condBlockStack.pop();
    PumaConditionalBlock *parent = _condBlockStack.top();
    _current->_end = node->startToken();
    _current = new PumaConditionalBlock(_file, parent, prev, node, _nodeNum++, *this);
    _current->_start = node->startToken();
    _file->push_back(_current);
    _condBlockStack.push(_current);
    parent->push_back(_current);
}

void PumaConditionalBlockBuilder::visitPreElseDirective_Pre (PreElseDirective *node) {
    TRACECALL;

    assert(_current);
    assert(_file);

    PumaConditionalBlock *prev = _condBlockStack.top();
    _condBlockStack.pop();
    PumaConditionalBlock *parent = _condBlockStack.top();
    _current->_end = node->startToken();
    _current = new PumaConditionalBlock(_file, parent, prev, node, _nodeNum++, *this);
    _current->_start = node->startToken();
    _file->push_back(_current);
    _condBlockStack.push(_current);
    parent->push_back(_current);
}

void PumaConditionalBlockBuilder::visitPreEndifDirective_Pre (__unused PreEndifDirective *node) {
    TRACECALL;

    _condBlockStack.pop();
    _current->_end = node->startToken();
    _current = _condBlockStack.top();
}

void PumaConditionalBlockBuilder::visitDefineHelper(PreTreeComposite *node, bool define) {
    const std::string definedFlag = node->son(1)->startToken()->text();
    const Puma::DString &definedDFlag = node->son(1)->startToken()->dtext();

    /* Don't handle function macros */
    if (cpp_parser()->macroManager()->getMacro(definedDFlag) != nullptr)
        return;

    PumaConditionalBlock &block = *_condBlockStack.top();

    CppFile::DefineMap &map = *_file->getDefines();
    auto i = map.find(definedFlag);

    if (i == map.end())
        // First define for this item
        map[definedFlag] = new CppDefine(&block, define, definedFlag);
    else
        (*i).second->newDefine(&block, define);

    block.addDefine(map[definedFlag]);
}

void PumaConditionalBlockBuilder::visitPreDefineConstantDirective_Pre (Puma::PreDefineConstantDirective *node){
    TRACECALL;
    visitDefineHelper(node, true);
}

void PumaConditionalBlockBuilder::visitPreUndefDirective_Pre (Puma::PreUndefDirective *node){
    TRACECALL;
    visitDefineHelper(node, false);

    const Puma::DString &definedFlag = node->son(1)->startToken()->dtext();
    cpp_parser()->macroManager()->removeMacro(definedFlag);
}

void PumaConditionalBlockBuilder::visitPreDefineFunctionDirective_Pre (Puma::PreDefineFunctionDirective * node){
    const Puma::DString &definedFlag = node->son(1)->startToken()->dtext();

    if (!_current->getParent()) { // Handle only toplevel defines
        if (node->sons() == 6) { // With parameter list
            char *expansion = buildString(node->son(5));

            auto macro = new PreMacro(node->son(1)->startToken()->dtext(),
                                                  node->son(3), expansion);
            delete[] expansion;
            cpp_parser()->macroManager ()->addMacro (macro);

        } else if (node->sons() == 5) { // Without parameter list
            char *expansion = buildString(node->son(4));

            auto macro = new PreMacro(node->son(1)->startToken()->dtext(),
                                                  (PreTree *) nullptr, expansion);
            delete[] expansion;
            cpp_parser()->macroManager ()->addMacro (macro);
        }
    } else {
        /* If an macro is defined in an block we can't expand them for
           sure anymore TODO Evaluate*/
        cpp_parser()->macroManager()->removeMacro(definedFlag);
    }
}

/************************************************************************/
/* PumaConditionalBlockBuilder specific helper functions                */
/************************************************************************/

static inline Puma::Token *next_non_whitespace_token(Puma::Unit *unit, Puma::Token *s) {
    do {
        s = unit->next(s);
    } while (s && s->is_whitespace());
    return s;
}

static inline std::string makro_transformation(Puma::Unit *unit, Puma::Token *s) {
    std::ostringstream os;
    if (!strcmp(s->text(), "IS_BUILTIN")) {
        os << "defined(" << unit->next(unit->next(s))->text() << ")";
    } else if (!strcmp(s->text(), "IS_MODULE")) {
        os << "defined(" << unit->next(unit->next(s))->text() << "_MODULE)";
    } else if (!strcmp(s->text(), "IS_ENABLED")) {
        const char *txt = unit->next(unit->next(s))->text();
        os << "(defined(" << txt << ") || defined(" << txt << "_MODULE))";
    } else {
        assert(false); // should never happen
    }
    return os.str();
}

static inline bool is_relevant_makro(Puma::Token *s) {
    return !strcmp(s->text(), "IS_BUILTIN") || !strcmp(s->text(), "IS_MODULE")
            || !strcmp(s->text(), "IS_ENABLED");
}

static inline Puma::Token * puma_token_next_newline(Puma::Token *e, Puma::Unit *unit) {
    do {
        e = unit->next(e);
    } while (e && e->text()[0] != '\n' && !strchr(e->text(), '\n'));
    return e;
}

/// \brief cuts out all bad CPP statements
void remove_cpp_statements(Puma::Unit *unit) {
    Puma::ManipCommander mc;
    for (Puma::Token *s = unit->first(); s != unit->last(); s = unit->next(s)) {
        switch(s->type()) {
        case TOK_PRE_ASSERT:
        case TOK_PRE_ERROR:
            //        case TOK_PRE_INCLUDE:
        case TOK_PRE_INCLUDE_NEXT:
        case TOK_PRE_WARNING:
            mc.kill(s, puma_token_next_newline(s, unit));
        }
    }
    Puma::ManipError error = mc.valid();
    if (!error)
        mc.commit();
    else
        Logging::error("ERROR: ", error);
}

/// \brief replaces #define CONFIG_FOO 0 -> #undef CONFIG_FOO
void normalize_define_null(Puma::Unit *unit) {
    Puma::ManipCommander mc;
    Puma::ErrorStream err;
    for (Puma::Token *s = unit->first(); s != unit->last(); s=unit->next(s)) {
        if (!s->is_preprocessor())
            continue;

        if (s->type() == TOK_PRE_DEFINE) {
            // There is always a TOK_WSPACE. thus, one token has to be skipped
            Puma::Token *ident = unit->next(unit->next(s));
            Puma::Token *what = unit->next(unit->next(ident));

            if (ident->type() == Puma::TOK_ID && !strcmp(what->text(), "0")) {
                auto undef = new Puma::CUnit(err);
                mc.addBuffer(undef);
                // always set filename for Puma::CUnits
                undef->name(s->location().filename().name());
                *undef << "#undef " << *ident << std::endl << Puma::endu;
                mc.replace(s, puma_token_next_newline(s, unit),
                        undef->first(), undef->last());
            }
        }
    }
    Puma::ManipError error = mc.valid();
    if (!error)
        mc.commit();
    else
        Logging::error("ERROR: ", error);
}

/// \brief replaces IS_ENABLED/IS_BUILTIN/IS_MODULE - Makros
void normalize_defined_makros(Puma::Unit *unit) {
    Puma::ManipCommander mc;
    Puma::ErrorStream err;

    // TOK_ID = id 290; TOK_WSPACE = id 400
    for (Puma::Token *s = unit->first(); s != unit->last(); s = unit->next(s)) {
        if (s->type() == TOK_PRE_IF || s->type() == TOK_PRE_ELIF) {
            Puma::Token *lineEnd = puma_token_next_newline(s, unit);
            // an #if-condition ends when a newline is found
            // ("line continuations" aren't newlines in token representation)
            for (s = unit->next(unit->next(s)); s != lineEnd; s = unit->next(s)) {
                if (is_relevant_makro(s)) {
                    auto enabled = new Puma::CUnit(err);
                    mc.addBuffer(enabled);
                    // set filename, Puma drops the condition if tokens are anonymous in conditions
                    enabled->name(s->location().filename().name());
                    *enabled << makro_transformation(unit, s) << Puma::endu;
                    mc.replace(s, unit->next(unit->next(unit->next(s))),
                            enabled->first(), enabled->last());
                }
            }
        }
    }
    Puma::ManipError error = mc.valid();
    if (!error)
        mc.commit();
    else
        Logging::error("ERROR: ", error);
}

void print_tokens(Puma::Unit *unit) {
    for (Puma::Token *s = unit->first(); s != unit->last(); s = unit->next(s)) {
        std::cout << s->type() << " " << s->text() << std::endl;
        if(s->type() == Puma::TOK_IF) {
            Puma::Token *tmp = next_non_whitespace_token(unit, s);
            std::cout << "next tok" << tmp->type() << " " << tmp->text() << std::endl;
        }
//      " " << s->is_macro_op() << " " << s->is_preprocessor() << std::endl;
    }
}

void undertaker_normalizations(Puma::Unit *unit) {
    remove_cpp_statements(unit);
    normalize_define_null(unit);
    normalize_defined_makros(unit);
// print token text and numbers for debugging
//    print_tokens(unit);
// print all text after transformation, puma function
//    unit->print(std::cout);
}


std::list<std::string> PumaConditionalBlockBuilder::_includePaths;

void PumaConditionalBlockBuilder::addIncludePath(const char *path){
    _includePaths.push_back(path);
}

// Remove an possible include guard, this was copied from libPuma::PreFileIncluder
static void removeIncludeGuard(Unit *unit) {
    Token *guard = nullptr, *ifndef, *end_define, *endif;
    Token *tok = (Token*)unit->first ();
    // skip comments and whitespace
    while (tok && (tok->is_whitespace () || tok->is_comment ()))
        tok = (Token*)unit->next (tok);
    // the next token has to be #ifndef
    if (!(tok && tok->is_preprocessor () && tok->type () == TOK_PRE_IFNDEF))
        return;
    ifndef = tok;
    tok = (Token*)unit->next (tok);
    // now whitespace
    if (!(tok && tok->is_whitespace ()))
        return;
    tok = (Token*)unit->next (tok);
    // the next has be an identifier => the name of the guard macro
    if (!(tok && tok->is_identifier ()))
        return;
    guard = tok;
    tok = (Token*)unit->next (tok);
    // skip comments and whitespace
    while (tok && (tok->is_whitespace () || tok->is_comment ()))
        tok = (Token*)unit->next (tok);
    // the next token has to be #define
    if (!(tok && tok->is_preprocessor () && tok->type () == TOK_PRE_DEFINE))
        return;
    tok = (Token*)unit->next (tok);
    // now whitespace
    if (!(tok && tok->is_whitespace ()))
        return;
    tok = (Token*)unit->next (tok);
    // the next has be an identifier => the name of the guard macro
    if (!(tok && tok->is_identifier ()))
        return;
    // check if the identifier is our guard variable
    if (strcmp (tok->text (), guard->text ()) != 0)
        return;
    tok = (Token*)unit->next (tok);
    end_define = tok;
    // find the corresponding #endif
    int level = 1;
    while (tok) {
        if (tok->is_preprocessor ()) {
            if (tok->type () == TOK_PRE_IF || tok->type () == TOK_PRE_IFDEF ||
                tok->type () == TOK_PRE_IFNDEF)
                level++;
            else if (tok->type () == TOK_PRE_ENDIF) {
                endif = tok;
                level--;
                if (level == 0)
                    break;
            }
        }
        tok = (Token*)unit->next (tok);
    }
    if (level > 0)
        return;
    tok = (Token*)unit->next (tok);
    // skip comments and whitespace
    while (tok && (tok->is_whitespace () || tok->is_comment ()))
        tok = (Token*)unit->next (tok);
    // here we should have reached the end of the unit!
    if (tok)
        return;

    Puma::ManipCommander mc;
    mc.kill(ifndef, end_define);
    mc.kill(endif, unit->last());
    mc.commit();
}

void PumaConditionalBlockBuilder::resolve_includes(Puma::Unit *unit) {
    Puma::PreFileIncluder includer(*_cpp);
    Puma::ManipCommander mc;
    Puma::Token *s, *e;
    std::string include;
    std::set<Puma::Unit *> already_seen;

    for (const std::string &str : _includePaths)
        includer.addIncludePath(str.c_str());

    for (s = unit->first(); s != unit->last() && s; s = unit->next(s)) {
        if (s->type() == TOK_PRE_INCLUDE) {
            e = s;
            include.clear();
            do {
                e = unit->next(e);
                include += e->text();
            } while (unit->next(e) && unit->next(e)->text()[0] != '\n');

            Puma::Unit *file = includer.includeFile(include.c_str());
            Puma::Token *before = unit->prev(s);
            if (file && already_seen.count(file) == 0) {
                /* Paste the included file only, if we haven't it seen until then */
                removeIncludeGuard(file);
                mc.paste_before(s, file);
                already_seen.insert(file);
            }
            mc.kill(s, e);
            mc.commit();
            /* Jump before the included file */
            s = before ? before : unit->first();
        }
    }
}

void PumaConditionalBlockBuilder::reset_MacroManager(Puma::Unit *unit) {
    Puma::Token *s, *e;

    for (s = unit->first(); s != unit->last(); s = unit->next(s)) {
        if (s->type() == TOK_PRE_DEFINE) {
            e = s;
            do {
                e = unit->next(e);
            } while (e->is_whitespace());
            cpp_parser()->macroManager()->removeMacro(e->dtext());
        }
    }
}

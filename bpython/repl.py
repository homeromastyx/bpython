# The MIT License
#
# Copyright (c) 2009-2011 the bpython authors.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import with_statement
import code
import errno
import inspect
import io
import os
import pkgutil
import pydoc
import requests
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import unicodedata
from itertools import takewhile
from locale import getpreferredencoding
from string import Template
from six import itervalues
from six.moves.urllib_parse import quote as urlquote, urljoin, urlparse

from pygments.token import Token

from bpython import inspection
from bpython._py3compat import PythonLexer, py3, prepare_for_exec
from bpython.formatter import Parenthesis
from bpython.translations import _, ngettext
from bpython.clipboard import get_clipboard, CopyFailed
from bpython.history import History
import bpython.autocomplete as autocomplete


class RuntimeTimer(object):
    def __init__(self):
        self.reset_timer()
        self.time = time.monotonic if hasattr(time, 'monotonic') else time.time

    def __enter__(self):
        self.start = self.time()

    def __exit__(self, ty, val, tb):
        self.last_command = self.time() - self.start
        self.running_time += self.last_command
        return False

    def reset_timer(self):
        self.running_time = 0.0
        self.last_command = 0.0

    def estimate(self):
        return self.running_time - self.last_command


class Interpreter(code.InteractiveInterpreter):

    def __init__(self, locals=None, encoding=None):
        """The syntaxerror callback can be set at any time and will be called
        on a caught syntax error. The purpose for this in bpython is so that
        the repl can be instantiated after the interpreter (which it
        necessarily must be with the current factoring) and then an exception
        callback can be added to the Interpeter instance afterwards - more
        specifically, this is so that autoindentation does not occur after a
        traceback."""

        self.encoding = encoding or sys.getdefaultencoding()
        self.syntaxerror_callback = None
        # Unfortunately code.InteractiveInterpreter is a classic class, so no
        # super()
        code.InteractiveInterpreter.__init__(self, locals)
        self.timer = RuntimeTimer()

    def reset_running_time(self):
        self.running_time = 0

    def runsource(self, source, filename='<input>', symbol='single',
                  encode=True):
        """Execute Python code.

        source, filename and symbol are passed on to
        code.InteractiveInterpreter.runsource. If encode is True, the source
        will be encoded. On Python 3.X, encode will be ignored."""
        if not py3 and encode:
            source = u'# coding: %s\n%s' % (self.encoding, source)
            source = source.encode(self.encoding)
        with self.timer:
            return code.InteractiveInterpreter.runsource(self, source,
                                                         filename, symbol)

    def showsyntaxerror(self, filename=None):
        """Override the regular handler, the code's copied and pasted from
        code.py, as per showtraceback, but with the syntaxerror callback called
        and the text in a pretty colour."""
        if self.syntaxerror_callback is not None:
            self.syntaxerror_callback()

        type, value, sys.last_traceback = sys.exc_info()
        sys.last_type = type
        sys.last_value = value
        if filename and type is SyntaxError:
            # Work hard to stuff the correct filename in the exception
            try:
                msg, (dummy_filename, lineno, offset, line) = value.args
            except:
                # Not the format we expect; leave it alone
                pass
            else:
                # Stuff in the right filename and right lineno
                if not py3:
                    lineno -= 1
                value = SyntaxError(msg, (filename, lineno, offset, line))
                sys.last_value = value
        list = traceback.format_exception_only(type, value)
        self.writetb(list)

    def showtraceback(self):
        """This needs to override the default traceback thing
        so it can put it into a pretty colour and maybe other
        stuff, I don't know"""
        try:
            t, v, tb = sys.exc_info()
            sys.last_type = t
            sys.last_value = v
            sys.last_traceback = tb
            tblist = traceback.extract_tb(tb)
            del tblist[:1]
            # Set the right lineno (encoding header adds an extra line)
            if not py3:
                for i, (fname, lineno, module, something) in enumerate(tblist):
                    if fname == '<input>':
                        tblist[i] = (fname, lineno - 1, module, something)

            l = traceback.format_list(tblist)
            if l:
                l.insert(0, "Traceback (most recent call last):\n")
            l[len(l):] = traceback.format_exception_only(t, v)
        finally:
            tblist = tb = None

        self.writetb(l)

    def writetb(self, lines):
        """This outputs the traceback and should be overridden for anything
        fancy."""
        for line in lines:
            self.write(line)


class MatchesIterator(object):
    """Stores a list of matches and which one is currently selected if any.

    Also responsible for doing the actual replacement of the original line with
    the selected match.

    A MatchesIterator can be `clear`ed to reset match iteration, and
    `update`ed to set what matches will be iterated over."""

    def __init__(self):
        # word being replaced in the original line of text
        self.current_word = ''
        # possible replacements for current_word
        self.matches = None
        # which word is currently replacing the current word
        self.index = -1
        # cursor position in the original line
        self.orig_cursor_offset = None
        # original line (before match replacements)
        self.orig_line = None
        # class describing the current type of completion
        self.completer = None

    def __nonzero__(self):
        """MatchesIterator is False when word hasn't been replaced yet"""
        return self.index != -1

    def __bool__(self):
        return self.index != -1

    @property
    def candidate_selected(self):
        """True when word selected/replaced, False when word hasn't been
        replaced yet"""
        return bool(self)

    def __iter__(self):
        return self

    def current(self):
        if self.index == -1:
            raise ValueError('No current match.')
        return self.matches[self.index]

    def next(self):
        return self.__next__()

    def __next__(self):
        self.index = (self.index + 1) % len(self.matches)
        return self.matches[self.index]

    def previous(self):
        if self.index <= 0:
            self.index = len(self.matches)
        self.index -= 1

        return self.matches[self.index]

    def cur_line(self):
        """Returns a cursor offset and line with the current substitution
        made"""
        return self.substitute(self.current())

    def substitute(self, match):
        """Returns a cursor offset and line with match substituted in"""
        start, end, word = self.completer.locate(self.orig_cursor_offset,
                                                 self.orig_line)
        return (start + len(match),
                self.orig_line[:start] + match + self.orig_line[end:])

    def is_cseq(self):
        return bool(
            os.path.commonprefix(self.matches)[len(self.current_word):])

    def substitute_cseq(self):
        """Returns a new line by substituting a common sequence in, and update
        matches"""
        cseq = os.path.commonprefix(self.matches)
        new_cursor_offset, new_line = self.substitute(cseq)
        if len(self.matches) == 1:
            self.clear()
        else:
            self.update(new_cursor_offset, new_line, self.matches,
                        self.completer)
            if len(self.matches) == 1:
                self.clear()
        return new_cursor_offset, new_line

    def update(self, cursor_offset, current_line, matches, completer):
        """Called to reset the match index and update the word being replaced

        Should only be called if there's a target to update - otherwise, call
        clear"""

        if matches is None:
            raise ValueError("Matches may not be None.")

        self.orig_cursor_offset = cursor_offset
        self.orig_line = current_line
        self.matches = matches
        self.completer = completer
        self.index = -1
        self.start, self.end, self.current_word = self.completer.locate(
            self.orig_cursor_offset, self.orig_line)

    def clear(self):
        self.matches = []
        self.cursor_offset = -1
        self.current_line = ''
        self.current_word = ''
        self.start = None
        self.end = None
        self.index = -1


class Interaction(object):
    def __init__(self, config, statusbar=None):
        self.config = config

        if statusbar:
            self.statusbar = statusbar

    def confirm(self, s):
        raise NotImplementedError

    def notify(self, s, n=10, wait_for_keypress=False):
        raise NotImplementedError

    def file_prompt(self, s):
        raise NotImplementedError


class SourceNotFound(Exception):
    """Exception raised when the requested source could not be found."""


class Repl(object):
    """Implements the necessary guff for a Python-repl-alike interface

    The execution of the code entered and all that stuff was taken from the
    Python code module, I had to copy it instead of inheriting it, I can't
    remember why. The rest of the stuff is basically what makes it fancy.

    It reads what you type, passes it to a lexer and highlighter which
    returns a formatted string. This then gets passed to echo() which
    parses that string and prints to the curses screen in appropriate
    colours and/or bold attribute.

    The Repl class also keeps two stacks of lines that the user has typed in:
    One to be used for the undo feature. I am not happy with the way this
    works.  The only way I have been able to think of is to keep the code
    that's been typed in in memory and re-evaluate it in its entirety for each
    "undo" operation. Obviously this means some operations could be extremely
    slow.  I'm not even by any means certain that this truly represents a
    genuine "undo" implementation, but it does seem to be generally pretty
    effective.

    If anyone has any suggestions for how this could be improved, I'd be happy
    to hear them and implement it/accept a patch. I researched a bit into the
    idea of keeping the entire Python state in memory, but this really seems
    very difficult (I believe it may actually be impossible to work) and has
    its own problems too.

    The other stack is for keeping a history for pressing the up/down keys
    to go back and forth between lines.

    XXX Subclasses should implement echo, current_line, cw
    """

    def __init__(self, interp, config):
        """Initialise the repl.

        interp is a Python code.InteractiveInterpreter instance

        config is a populated bpython.config.Struct.
        """

        self.config = config
        self.cut_buffer = ''
        self.buffer = []
        self.interp = interp
        self.interp.syntaxerror_callback = self.clear_current_line
        self.match = False
        self.rl_history = History(duplicates=config.hist_duplicates,
                                  hist_size=config.hist_length)
        self.s_hist = []
        self.history = []
        self.evaluating = False
        self.matches_iter = MatchesIterator()
        self.argspec = None
        self.current_func = None
        self.highlighted_paren = None
        self._C = {}
        self.prev_block_finished = 0
        self.interact = Interaction(self.config)
        # previous pastebin content to prevent duplicate pastes, filled on call
        # to repl.pastebin
        self.prev_pastebin_content = ''
        self.prev_pastebin_url = ''
        self.prev_removal_url = ''
        # Necessary to fix mercurial.ui.ui expecting sys.stderr to have this
        # attribute
        self.closed = False
        self.clipboard = get_clipboard()

        pythonhist = os.path.expanduser(self.config.hist_file)
        if os.path.exists(pythonhist):
            try:
                self.rl_history.load(pythonhist,
                                     getpreferredencoding() or "ascii")
            except EnvironmentError:
                pass

    @property
    def ps1(self):
        try:
            return str(sys.ps1)
        except AttributeError:
            return '>>> '

    @property
    def ps2(self):
        try:
            return str(sys.ps2)
        except AttributeError:
            return '... '

    def startup(self):
        """
        Execute PYTHONSTARTUP file if it exits. Call this after front
        end-specific initialisation.
        """
        filename = os.environ.get('PYTHONSTARTUP')
        if filename:
            encoding = inspection.get_encoding_file(filename)
            with io.open(filename, 'rt', encoding=encoding) as f:
                source = f.read()
                if not py3:
                    # Python 2.6 and early 2.7.X need bytes.
                    source = source.encode(encoding)
                self.interp.runsource(source, filename, 'exec', encode=False)

    def current_string(self, concatenate=False):
        """If the line ends in a string get it, otherwise return ''"""
        tokens = self.tokenize(self.current_line)
        string_tokens = list(takewhile(token_is_any_of([Token.String,
                                                        Token.Text]),
                                       reversed(tokens)))
        if not string_tokens:
            return ''
        opening = string_tokens.pop()[1]
        string = list()
        for (token, value) in reversed(string_tokens):
            if token is Token.Text:
                continue
            elif opening is None:
                opening = value
            elif token is Token.String.Doc:
                string.append(value[3:-3])
                opening = None
            elif value == opening:
                opening = None
                if not concatenate:
                    string = list()
            else:
                string.append(value)

        if opening is None:
            return ''
        return ''.join(string)

    def get_object(self, name):
        attributes = name.split('.')
        obj = eval(attributes.pop(0), self.interp.locals)
        while attributes:
            with inspection.AttrCleaner(obj):
                obj = getattr(obj, attributes.pop(0))
        return obj

    def get_args(self):
        """Check if an unclosed parenthesis exists, then attempt to get the
        argspec() for it. On success, update self.argspec and return True,
        otherwise set self.argspec to None and return False"""

        self.current_func = None

        if not self.config.arg_spec:
            return False

        # Get the name of the current function and where we are in
        # the arguments
        stack = [['', 0, '']]
        try:
            for (token, value) in PythonLexer().get_tokens(
                    self.current_line):
                if token is Token.Punctuation:
                    if value in '([{':
                        stack.append(['', 0, value])
                    elif value in ')]}':
                        stack.pop()
                    elif value == ',':
                        try:
                            stack[-1][1] += 1
                        except TypeError:
                            stack[-1][1] = ''
                        stack[-1][0] = ''
                    elif value == ':' and stack[-1][2] == 'lambda':
                        stack.pop()
                    else:
                        stack[-1][0] = ''
                elif (token is Token.Name or token in Token.Name.subtypes or
                      token is Token.Operator and value == '.'):
                    stack[-1][0] += value
                elif token is Token.Operator and value == '=':
                    stack[-1][1] = stack[-1][0]
                    stack[-1][0] = ''
                elif token is Token.Keyword and value == 'lambda':
                    stack.append(['', 0, value])
                else:
                    stack[-1][0] = ''
            while stack[-1][2] in '[{':
                stack.pop()
            _, arg_number, _ = stack.pop()
            func, _, _ = stack.pop()
        except IndexError:
            return False
        if not func:
            return False

        try:
            f = self.get_object(func)
        except Exception:
            # another case of needing to catch every kind of error
            # since user code is run in the case of descriptors
            # XXX: Make sure you raise here if you're debugging the completion
            # stuff !
            return False

        if inspect.isclass(f):
            try:
                if f.__init__ is not object.__init__:
                    f = f.__init__
            except AttributeError:
                return None
        self.current_func = f

        self.argspec = inspection.getargspec(func, f)
        if self.argspec:
            self.argspec.append(arg_number)
            return True
        return False

    def get_source_of_current_name(self):
        """Return the unicode source code of the object which is bound to the
        current name in the current input line. Throw `SourceNotFound` if the
        source cannot be found."""

        obj = self.current_func
        try:
            if obj is None:
                line = self.current_line
                if not line.strip():
                    raise SourceNotFound(_("Nothing to get source of"))
                if inspection.is_eval_safe_name(line):
                    obj = self.get_object(line)
            return inspection.get_source_unicode(obj)
        except (AttributeError, NameError) as e:
            msg = _("Cannot get source: %s") % (str(e), )
        except IOError as e:
            msg = str(e)
        except TypeError as e:
            if "built-in" in str(e):
                msg = _("Cannot access source of %r") % (obj, )
            else:
                msg = _("No source code found for %s") % (self.current_line, )
        raise SourceNotFound(msg)

    def set_docstring(self):
        self.docstring = None
        if not self.get_args():
            self.argspec = None
        elif self.current_func is not None:
            try:
                self.docstring = pydoc.getdoc(self.current_func)
            except IndexError:
                self.docstring = None
            else:
                # pydoc.getdoc() returns an empty string if no
                # docstring was found
                if not self.docstring:
                    self.docstring = None

    # What complete() does:
    # Should we show the completion box? (are there matches, or is there a
    # docstring to show?)
    #   Some completions should always be shown, other only if tab=True
    # set the current docstring to the "current function's" docstring
    # Populate the matches_iter object with new matches from the current state
    #    if none, clear the matches iterator
    # If exactly one match that is equal to current line, clear matches
    # If example one match and tab=True, then choose that and clear matches

    def complete(self, tab=False):
        """Construct a full list of possible completions and
        display them in a window. Also check if there's an available argspec
        (via the inspect module) and bang that on top of the completions too.
        The return value is whether the list_win is visible or not.

        If no matches are found, just return whether there's an argspec to show
        If any matches are found, save them and select the first one.

        If tab is True exactly one match found, make the replacement and return
          the result of running complete() again on the new line.
        """

        self.set_docstring()

        matches, completer = autocomplete.get_completer_bpython(
            cursor_offset=self.cursor_offset,
            line=self.current_line,
            locals_=self.interp.locals,
            argspec=self.argspec,
            current_block='\n'.join(self.buffer + [self.current_line]),
            complete_magic_methods=self.config.complete_magic_methods,
            history=self.history)
        # TODO implement completer.shown_before_tab == False (filenames
        # shouldn't fill screen)

        if len(matches) == 0:
            self.matches_iter.clear()
            return bool(self.argspec)

        self.matches_iter.update(self.cursor_offset,
                                 self.current_line, matches, completer)

        if len(matches) == 1:
            if tab:
                # if this complete is being run for a tab key press, substitute
                # common sequence
                self._cursor_offset, self._current_line = \
                    self.matches_iter.substitute_cseq()
                return Repl.complete(self)  # again for
            elif self.matches_iter.current_word == matches[0]:
                self.matches_iter.clear()
                return False
            return completer.shown_before_tab

        else:
            assert len(matches) > 1
            return tab or completer.shown_before_tab

    def format_docstring(self, docstring, width, height):
        """Take a string and try to format it into a sane list of strings to be
        put into the suggestion box."""

        lines = docstring.split('\n')
        out = []
        i = 0
        for line in lines:
            i += 1
            if not line.strip():
                out.append('\n')
            for block in textwrap.wrap(line, width):
                out.append('  ' + block + '\n')
                if i >= height:
                    return out
                i += 1
        # Drop the last newline
        out[-1] = out[-1].rstrip()
        return out

    def next_indentation(self):
        """Return the indentation of the next line based on the current
        input buffer."""
        if self.buffer:
            indentation = next_indentation(self.buffer[-1],
                                           self.config.tab_length)
            if indentation and self.config.dedent_after > 0:
                line_is_empty = lambda line: not line.strip()
                empty_lines = takewhile(line_is_empty, reversed(self.buffer))
                if sum(1 for _ in empty_lines) >= self.config.dedent_after:
                    indentation -= 1
        else:
            indentation = 0
        return indentation

    def formatforfile(self, s):
        """Format the stdout buffer to something suitable for writing to disk,
        i.e. without >>> and ... at input lines and with "# OUT: " prepended to
        output lines."""

        def process():
            for line in s.split('\n'):
                if line.startswith(self.ps1):
                    yield line[len(self.ps1):]
                elif line.startswith(self.ps2):
                    yield line[len(self.ps2):]
                elif line.rstrip():
                    yield "# OUT: %s" % (line,)
        return "\n".join(process())

    def write2file(self):
        """Prompt for a filename and write the current contents of the stdout
        buffer to disk."""

        try:
            fn = self.interact.file_prompt(_('Save to file (Esc to cancel): '))
            if not fn:
                self.interact.notify(_('Save cancelled.'))
                return
        except ValueError:
            self.interact.notify(_('Save cancelled.'))
            return

        if fn.startswith('~'):
            fn = os.path.expanduser(fn)
        if not fn.endswith('.py') and self.config.save_append_py:
            fn = fn + '.py'

        mode = 'w'
        if os.path.exists(fn):
            mode = self.interact.file_prompt(_('%s already exists. Do you '
                                               'want to (c)ancel, '
                                               ' (o)verwrite or '
                                               '(a)ppend? ') % (fn, ))
            if mode in ('o', 'overwrite', _('overwrite')):
                mode = 'w'
            elif mode in ('a', 'append', _('append')):
                mode = 'a'
            else:
                self.interact.notify(_('Save cancelled.'))
                return

        s = self.formatforfile(self.getstdout())

        try:
            with open(fn, mode) as f:
                f.write(s)
        except IOError as e:
            self.interact.notify(_("Error writing file '%s': %s") % (fn,
                                                                     str(e)))
        else:
            self.interact.notify(_('Saved to %s.') % (fn, ))

    def copy2clipboard(self):
        """Copy current content to clipboard."""

        if self.clipboard is None:
            self.interact.notify(_('No clipboard available.'))
            return

        content = self.formatforfile(self.getstdout())
        try:
            self.clipboard.copy(content)
        except CopyFailed:
            self.interact.notify(_('Could not copy to clipboard.'))
        else:
            self.interact.notify(_('Copied content to clipboard.'))

    def pastebin(self, s=None):
        """Upload to a pastebin and display the URL in the status bar."""

        if s is None:
            s = self.getstdout()

        if (self.config.pastebin_confirm and
                not self.interact.confirm(_("Pastebin buffer? (y/N) "))):
            self.interact.notify(_("Pastebin aborted."))
            return
        return self.do_pastebin(s)

    def do_pastebin(self, s):
        """Actually perform the upload."""
        if s == self.prev_pastebin_content:
            self.interact.notify(_('Duplicate pastebin. Previous URL: %s. '
                                   'Removal URL: %s') %
                                  (self.prev_pastebin_url,
                                   self.prev_removal_url), 10)
            return self.prev_pastebin_url

        if self.config.pastebin_helper:
            return self.do_pastebin_helper(s)
        else:
            return self.do_pastebin_json(s)

    def do_pastebin_json(self, s):
        """Upload to pastebin via json interface."""

        url = urljoin(self.config.pastebin_url, '/json/new')
        payload = {
            'code': s,
            'lexer': 'pycon',
            'expiry': self.config.pastebin_expiry
        }

        self.interact.notify(_('Posting data to pastebin...'))
        try:
            response = requests.post(url, data=payload, verify=True)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            self.interact.notify(_('Upload failed: %s') % (str(exc), ))
            return

        self.prev_pastebin_content = s
        data = response.json()

        paste_url_template = Template(self.config.pastebin_show_url)
        paste_id = urlquote(data['paste_id'])
        paste_url = paste_url_template.safe_substitute(paste_id=paste_id)

        removal_url_template = Template(self.config.pastebin_removal_url)
        removal_id = urlquote(data['removal_id'])
        removal_url = removal_url_template.safe_substitute(
            removal_id=removal_id)

        self.prev_pastebin_url = paste_url
        self.prev_removal_url = removal_url
        self.interact.notify(_('Pastebin URL: %s - Removal URL: %s') %
                             (paste_url, removal_url), 10)

        return paste_url

    def do_pastebin_helper(self, s):
        """Call out to helper program for pastebin upload."""
        self.interact.notify(_('Posting data to pastebin...'))

        try:
            helper = subprocess.Popen('',
                                      executable=self.config.pastebin_helper,
                                      stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE)
            helper.stdin.write(s.encode(getpreferredencoding()))
            output = helper.communicate()[0].decode(getpreferredencoding())
            paste_url = output.split()[0]
        except OSError as e:
            if e.errno == errno.ENOENT:
                self.interact.notify(_('Upload failed: '
                                       'Helper program not found.'))
            else:
                self.interact.notify(_('Upload failed: '
                                       'Helper program could not be run.'))
            return

        if helper.returncode != 0:
            self.interact.notify(_('Upload failed: '
                                   'Helper program returned non-zero exit '
                                   'status %d.' % (helper.returncode, )))
            return

        if not paste_url:
            self.interact.notify(_('Upload failed: '
                                   'No output from helper program.'))
            return
        else:
            parsed_url = urlparse(paste_url)
            if (not parsed_url.scheme
                    or any(unicodedata.category(c) == 'Cc'
                           for c in paste_url)):
                self.interact.notify(_("Upload failed: "
                                       "Failed to recognize the helper "
                                       "program's output as an URL."))
                return

        self.prev_pastebin_content = s
        self.interact.notify(_('Pastebin URL: %s') % (paste_url, ), 10)
        return paste_url

    def push(self, s, insert_into_history=True):
        """Push a line of code onto the buffer so it can process it all
        at once when a code block ends"""
        s = s.rstrip('\n')
        self.buffer.append(s)

        if insert_into_history:
            self.insert_into_history(s)

        more = self.interp.runsource('\n'.join(self.buffer))

        if not more:
            self.buffer = []

        return more

    def insert_into_history(self, s):
        try:
            self.rl_history.append_reload_and_write(s, self.config.hist_file,
                                                    getpreferredencoding())
        except RuntimeError as e:
            self.interact.notify(str(e))

    def prompt_undo(self):
        """Returns how many lines to undo, 0 means don't undo"""
        if (self.config.single_undo_time < 0 or
                self.interp.timer.estimate() < self.config.single_undo_time):
            return 1
        est = self.interp.timer.estimate()
        n = self.interact.file_prompt(
            _("Undo how many lines? (Undo will take up to ~%.1f seconds) [1]")
            % (est,))
        try:
            if n == '':
                n = '1'
            n = int(n)
        except ValueError:
            self.interact.notify(_('Undo canceled'), .1)
            return 0
        else:
            if n == 0:
                self.interact.notify(_('Undo canceled'), .1)
                return 0
            else:
                message = ngettext('Undoing %d line... (est. %.1f seconds)',
                                   'Undoing %d lines... (est. %.1f seconds)',
                                   n)
                self.interact.notify(message % (n, est), .1)
            return n

    def undo(self, n=1):
        """Go back in the undo history n steps and call reevaluate()
        Note that in the program this is called "Rewind" because I
        want it to be clear that this is by no means a true undo
        implementation, it is merely a convenience bonus."""
        if not self.history:
            return None

        self.interp.timer.reset_timer()

        if len(self.history) < n:
            n = len(self.history)

        entries = list(self.rl_history.entries)

        self.history = self.history[:-n]
        self.reevaluate()

        self.rl_history.entries = entries

    def flush(self):
        """Olivier Grisel brought it to my attention that the logging
        module tries to call this method, since it makes assumptions
        about stdout that may not necessarily be true. The docs for
        sys.stdout say:

        "stdout and stderr needn't be built-in file objects: any
         object is acceptable as long as it has a write() method
         that takes a string argument."

        So I consider this to be a bug in logging, and this is a hack
        to fix it, unfortunately. I'm sure it's not the only module
        to do it."""

    def close(self):
        """See the flush() method docstring."""

    def tokenize(self, s, newline=False):
        """Tokenizes a line of code, returning pygments tokens
        with side effects/impurities:
        - reads self.cpos to see what parens should be highlighted
        - reads self.buffer to see what came before the passed in line
        - sets self.highlighted_paren to (buffer_lineno, tokens_for_that_line)
          for buffer line that should replace that line to unhighlight it,
          or None if no paren is currently highlighted
        - calls reprint_line with a buffer's line's tokens and the buffer
          lineno that has changed if line other than the current line changes
        """
        highlighted_paren = None

        source = '\n'.join(self.buffer + [s])
        cursor = len(source) - self.cpos
        if self.cpos:
            cursor += 1
        stack = list()
        all_tokens = list(PythonLexer().get_tokens(source))
        # Unfortunately, Pygments adds a trailing newline and strings with
        # no size, so strip them
        while not all_tokens[-1][1]:
            all_tokens.pop()
        all_tokens[-1] = (all_tokens[-1][0], all_tokens[-1][1].rstrip('\n'))
        line = pos = 0
        parens = dict(zip('{([', '})]'))
        line_tokens = list()
        saved_tokens = list()
        search_for_paren = True
        for (token, value) in split_lines(all_tokens):
            pos += len(value)
            if token is Token.Text and value == '\n':
                line += 1
                # Remove trailing newline
                line_tokens = list()
                saved_tokens = list()
                continue
            line_tokens.append((token, value))
            saved_tokens.append((token, value))
            if not search_for_paren:
                continue
            under_cursor = (pos == cursor)
            if token is Token.Punctuation:
                if value in parens:
                    if under_cursor:
                        line_tokens[-1] = (Parenthesis.UnderCursor, value)
                        # Push marker on the stack
                        stack.append((Parenthesis, value))
                    else:
                        stack.append((line, len(line_tokens) - 1,
                                      line_tokens, value))
                elif value in itervalues(parens):
                    saved_stack = list(stack)
                    try:
                        while True:
                            opening = stack.pop()
                            if parens[opening[-1]] == value:
                                break
                    except IndexError:
                        # SyntaxError.. more closed parentheses than
                        # opened or a wrong closing paren
                        opening = None
                        if not saved_stack:
                            search_for_paren = False
                        else:
                            stack = saved_stack
                    if opening and opening[0] is Parenthesis:
                        # Marker found
                        line_tokens[-1] = (Parenthesis, value)
                        search_for_paren = False
                    elif opening and under_cursor and not newline:
                        if self.cpos:
                            line_tokens[-1] = (Parenthesis.UnderCursor, value)
                        else:
                            # The cursor is at the end of line and next to
                            # the paren, so it doesn't reverse the paren.
                            # Therefore, we insert the Parenthesis token
                            # here instead of the Parenthesis.UnderCursor
                            # token.
                            line_tokens[-1] = (Parenthesis, value)
                        (lineno, i, tokens, opening) = opening
                        if lineno == len(self.buffer):
                            highlighted_paren = (lineno, saved_tokens)
                            line_tokens[i] = (Parenthesis, opening)
                        else:
                            highlighted_paren = (lineno, list(tokens))
                            # We need to redraw a line
                            tokens[i] = (Parenthesis, opening)
                            self.reprint_line(lineno, tokens)
                        search_for_paren = False
                elif under_cursor:
                    search_for_paren = False
        self.highlighted_paren = highlighted_paren
        if line != len(self.buffer):
            return list()
        return line_tokens

    def clear_current_line(self):
        """This is used as the exception callback for the Interpreter instance.
        It prevents autoindentation from occuring after a traceback."""

    def send_to_external_editor(self, text, filename=None):
        """Returns modified text from an editor, or the oriignal text if editor
        exited with non-zero"""

        encoding = getpreferredencoding()
        editor_args = shlex.split(prepare_for_exec(self.config.editor,
                                                   encoding))
        with tempfile.NamedTemporaryFile(suffix='.py') as temp:
            temp.write(text.encode(encoding))
            temp.flush()

            args = editor_args + [prepare_for_exec(temp.name, encoding)]
            if subprocess.call(args) == 0:
                with open(temp.name) as f:
                    if py3:
                        return f.read()
                    else:
                        return f.read().decode(encoding)
            else:
                return text

    def open_in_external_editor(self, filename):
        encoding = getpreferredencoding()
        editor_args = shlex.split(prepare_for_exec(self.config.editor,
                                                   encoding))
        args = editor_args + [prepare_for_exec(filename, encoding)]
        if subprocess.call(args) == 0:
            return True
        return False

    def edit_config(self):
        if not (os.path.isfile(self.config.config_path)):
            if self.interact.confirm(_("Config file does not exist - create "
                                       "new from default? (y/N)")):
                try:
                    default_config = pkgutil.get_data('bpython',
                                                      'sample-config')
                    bpython_dir, script_name = os.path.split(__file__)
                    containing_dir = os.path.dirname(
                        os.path.abspath(self.config.config_path))
                    if not os.path.exists(containing_dir):
                        os.makedirs(containing_dir)
                    with open(self.config.config_path, 'w') as f:
                        f.write(default_config)
                except (IOError, OSError) as e:
                    self.interact.notify(_("Error writing file '%s': %s") %
                                         (self.config.config.path, str(e)))
                    return False
            else:
                return False

        if self.open_in_external_editor(self.config.config_path):
            self.interact.notify(_('bpython config file edited. Restart '
                                   'bpython for changes to take effect.'))
        else:
            self.interact.notify(_('Error editing config file.'))


def next_indentation(line, tab_length):
    """Given a code line, return the indentation of the next line."""
    line = line.expandtabs(tab_length)
    indentation = (len(line) - len(line.lstrip(' '))) // tab_length
    if line.rstrip().endswith(':'):
        indentation += 1
    elif indentation >= 1:
        if line.lstrip().startswith(('return', 'pass', 'raise', 'yield')):
            indentation -= 1
    return indentation


def next_token_inside_string(s, inside_string):
    """Given a code string s and an initial state inside_string, return
    whether the next token will be inside a string or not."""
    for token, value in PythonLexer().get_tokens(s):
        if token is Token.String:
            value = value.lstrip('bBrRuU')
            if value in ['"""', "'''", '"', "'"]:
                if not inside_string:
                    inside_string = value
                elif value == inside_string:
                    inside_string = False
    return inside_string


def split_lines(tokens):
    for (token, value) in tokens:
        if not value:
            continue
        while value:
            head, newline, value = value.partition('\n')
            yield (token, head)
            if newline:
                yield (Token.Text, newline)


def token_is(token_type):
    """Return a callable object that returns whether a token is of the
    given type `token_type`."""

    def token_is_type(token):
        """Return whether a token is of a certain type or not."""
        token = token[0]
        while token is not token_type and token.parent:
            token = token.parent
        return token is token_type

    return token_is_type


def token_is_any_of(token_types):
    """Return a callable object that returns whether a token is any of the
    given types `token_types`."""
    is_token_types = tuple(map(token_is, token_types))

    def token_is_any_of(token):
        return any(check(token) for check in is_token_types)

    return token_is_any_of


def extract_exit_value(args):
    """Given the arguments passed to `SystemExit`, return the value that
    should be passed to `sys.exit`.
    """
    if len(args) == 0:
        return None
    elif len(args) == 1:
        return args[0]
    else:
        return args

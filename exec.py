import collections
import functools
import html
import os
import subprocess
import sys
import threading
import time
import codecs
import signal
import datetime

import sublime
import sublime_plugin

g_is_panel_focused = False
g_last_scroll_positions = {}


# https://forum.sublimetext.com/t/how-to-set-focus-to-exec-output-panel/26689/5
# https://forum.sublimetext.com/t/how-to-track-if-an-output-panel-is-closed-hidden/8453/6
class ExecOutputFocusCancelBuildCommand(sublime_plugin.WindowCommand):

    def run(self):
        window = self.window
        active_panel = window.active_panel()

        if active_panel:
            panel_view = self.window.find_output_panel( active_panel ) or \
                    self.window.find_output_panel( active_panel.lstrip("output.") )

            if g_is_panel_focused:
                user_notice = "Cancelling the build for '%s'..." % active_panel
                print( str(datetime.datetime.now())[:-4], user_notice )

                sublime.status_message( user_notice )
                self.window.run_command( 'cancel_build' )

            else:
                self.window.focus_view( panel_view )

        else:
            self.window.run_command('show_panel', args={'panel': 'output.exec'})


class HackListener(sublime_plugin.EventListener):

    def on_activated(self, view):
        global g_is_panel_focused

        active_window = sublime.active_window()
        view_has_name = not not view.file_name()

        is_widget = view.settings().get('is_widget')
        active_panel = active_window.active_panel()
        g_is_panel_focused = active_panel and not view_has_name and not is_widget

        # print( "is_widget:", is_widget )
        # print( "view_has_name:", view_has_name )
        # print( "g_is_panel_focused:", g_is_panel_focused )


class ProcessListener(object):
    def on_data(self, proc, data):
        pass

    def on_finished(self, proc):
        pass


class AsyncProcess(object):
    """
    Encapsulates subprocess.Popen, forwarding stdout to a supplied
    ProcessListener (on a separate thread)
    """

    def __init__(self, cmd, shell_cmd, env, listener, path="", shell=False):
        """ "path" and "shell" are options in build systems """

        if not shell_cmd and not cmd:
            raise ValueError("shell_cmd or cmd is required")

        if shell_cmd and not isinstance(shell_cmd, str):
            raise ValueError("shell_cmd must be a string")

        self.listener = listener
        self.killed = False

        self.start_time = time.time()

        # Hide the console window on Windows
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        # Set temporary PATH to locate executable in cmd
        if path:
            old_path = os.environ["PATH"]
            # The user decides in the build system whether he wants to append $PATH
            # or tuck it at the front: "$PATH;C:\\new\\path", "C:\\new\\path;$PATH"
            os.environ["PATH"] = os.path.expandvars(path)

        proc_env = os.environ.copy()
        proc_env.update(env)
        for k, v in proc_env.items():
            proc_env[k] = os.path.expandvars(v)

        if sys.platform == "win32":
            preexec_fn = None
        else:
            preexec_fn = os.setsid

        if shell_cmd and sys.platform == "win32":
            # Use shell=True on Windows, so shell_cmd is passed through with the correct escaping
            self.proc = subprocess.Popen(
                shell_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                env=proc_env,
                shell=True)
        elif shell_cmd and sys.platform == "darwin":
            # Use a login shell on OSX, otherwise the users expected env vars won't be setup
            self.proc = subprocess.Popen(
                ["/usr/bin/env", "bash", "-l", "-c", shell_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                env=proc_env,
                preexec_fn=preexec_fn,
                shell=False)
        elif shell_cmd and sys.platform == "linux":
            # Explicitly use /bin/bash on Linux, to keep Linux and OSX as
            # similar as possible. A login shell is explicitly not used for
            # linux, as it's not required
            self.proc = subprocess.Popen(
                ["/usr/bin/env", "bash", "-c", shell_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                env=proc_env,
                preexec_fn=preexec_fn,
                shell=False)
        else:

            # Old style build system, just do what it asks
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                env=proc_env,
                preexec_fn=preexec_fn,
                shell=shell)

        if path:
            os.environ["PATH"] = old_path

        if self.proc.stdout:
            threading.Thread(
                target=self.read_fileno,
                args=(self.proc.stdout.fileno(), True)
            ).start()

        if self.proc.stderr:
            threading.Thread(
                target=self.read_fileno,
                args=(self.proc.stderr.fileno(), False)
            ).start()

    def kill(self):
        if not self.killed:
            self.killed = True
            if sys.platform == "win32":
                # terminate would not kill process opened by the shell cmd.exe,
                # it will only kill cmd.exe leaving the child running
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.Popen(
                    "taskkill /PID %d /T /F" % self.proc.pid,
                    startupinfo=startupinfo)
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.terminate()
            self.listener = None

    def poll(self):
        return self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll()

    def read_fileno(self, fileno, execute_finished):
        decoder_cls = codecs.getincrementaldecoder(self.listener.encoding)
        decoder = decoder_cls('replace')
        while True:
            data = decoder.decode(os.read(fileno, 2**16))

            if len(data) > 0:
                if self.listener:
                    self.listener.on_data(self, data)
            else:
                try:
                    os.close(fileno)
                except OSError:
                    pass
                if execute_finished and self.listener:
                    self.listener.on_finished(self)
                break


class FixSublimeTextOutputBuild(sublime_plugin.WindowCommand):

    def run(self, **kwargs) :
        window = self.window
        output_view = window.find_output_panel( "exec" )

        # We need to save the view positions before the builtin `build` command run, because it
        # immediately erases the view contents.
        self.saveViewPositions( window, output_view )

        window.run_command( 'build', kwargs )

    def saveViewPositions(self, window, output_view):

        if output_view:
            window_id = window.id()
            view_settings = window.active_view().settings()

            if view_settings.get( 'restore_output_view_scroll' , False ):

                g_last_scroll_positions[window_id] = (output_view.viewport_position(),
                        [(selection.begin(), selection.end()) for selection in output_view.sel()])

                # print( 'Before substr:                     ', output_view.substr(sublime.Region(0, 10)) )
                # print( 'Before window.id:                  ', window.id() )
                # print( 'Before output_view:                ', output_view )
                # print( 'Before output_view.id:             ', output_view.id() )
                # print( 'g_last_scroll_positions[window_id] ', g_last_scroll_positions[window_id] )

            else:
                # Force to disable the scroll restoring, if the setting is disabled after being enabled
                g_last_scroll_positions[window_id] = (None, None)


# exit_now = False
# def plugin_loaded():
#     def function():
#         global exit_now
#         exit_now = False
#         threading.Thread(target=save_output_view_scroll).start()
#     global exit_now
#     exit_now = True
#     sublime.set_timeout_async( function, 1000 )

# def save_output_view_scroll():
#     global exit_now
#     while True:
#         time.sleep(0.5)
#         if exit_now:
#             break
#         window = sublime.active_window()
#         output_view = window.find_output_panel("exec")
#         if output_view:
#             print('substr1:                 ', output_view.substr(sublime.Region(0, 10)))
#             print('window.id:               ', window.id())
#             print('output_view:             ', output_view)
#             print('output_view.id:          ', output_view.id())
#         else:
#             print('output_view:             ', output_view)


class ThreadProgress():
    """
    Animates an indicator, [=   ], in the status area while a thread runs

    Based on Package Control
    https://github.com/wbond/package_control/blob/db53090bd0920ca2c58ef27f0361a4d7b096df0e/package_control/thread_progress.py

    :param thread:
        The thread to track for activity

    :param message:
        The message to display next to the activity indicator

    :param success_message:
        The message to display once the thread is complete
    """
    windows = {}
    running = False

    def __init__(self, message, success_message):
        self.status_name = "output_build_view_"
        self.message = message
        self.success_message = success_message
        self.addend = 1
        self.size = 12
        self.last_view = None
        self.window = sublime.active_window()
        self.index = 0
        self.is_alive = True

        if self.window.id() in self.windows:
            print('Skipping ThreadProgress indicator because it is already running!')

        else:
            self.windows[self.window.id()] = self
            sublime.set_timeout(lambda: self.run(), 100)

    @classmethod
    def stop(cls):
        window_id = sublime.active_window().id()
        if window_id in cls.windows:
            progress = cls.windows[window_id]
            progress.is_alive = False
            del cls.windows[window_id]

    def run(self):
        active_view = self.window.active_view()
        active_window_id = '%s%s' % ( self.status_name, self.window.id() )

        if self.last_view is not None and active_view != self.last_view:
            self.last_view.erase_status(active_window_id)
            self.last_view = None

        if not self.is_alive:
            def cleanup():
                active_view.erase_status(active_window_id)

            active_view.set_status(active_window_id, self.success_message)
            sublime.set_timeout(cleanup, 10000)
            return

        before = self.index % self.size
        after = (self.size - 1) - before

        active_view.set_status(active_window_id, '%s [%s=%s]' % (self.message, ' ' * before, ' ' * after))
        if self.last_view is None:
            self.last_view = active_view

        if not after:
            self.addend = -1

        if not before:
            self.addend = 1

        self.index += self.addend
        sublime.set_timeout(lambda: self.run(), 100)


class ExecCommand(sublime_plugin.WindowCommand, ProcessListener):
    BLOCK_SIZE = 2**14
    text_queue = collections.deque()
    text_queue_proc = None
    text_queue_lock = threading.Lock()

    proc = None

    errs_by_file = {}
    phantom_sets_by_buffer = {}
    show_errors_inline = True

    def run(
            self,
            cmd=None,
            shell_cmd=None,
            file_regex="",
            line_regex="",
            working_dir="",
            encoding="utf-8",
            env={},
            quiet=False,
            kill=False,
            update_phantoms_only=False,
            hide_phantoms_only=False,
            output_build_word_wrap=None,
            spell_check=None,
            gutter=None,
            syntax="Packages/Text/Plain text.tmLanguage",
            # Catches "path" and "shell"
            **kwargs):
        # print( 'ExecCommand arguments: ', locals())
        view_settings = self.window.active_view().settings()

        if update_phantoms_only:
            if self.show_errors_inline:
                self.update_phantoms()
            return
        if hide_phantoms_only:
            self.hide_phantoms()
            return

        # clear the text_queue
        with self.text_queue_lock:
            self.text_queue.clear()
            self.text_queue_proc = None

        if kill:
            if self.proc:
                self.proc.kill()
                self.proc = None
                self.append_string(None, "[Cancelled]")
            return

        if not hasattr(self, 'output_view'):
            # Try not to call get_output_panel until the regexes are assigned
            self.output_view = self.window.create_output_panel("exec")

        # Default the to the current files directory if no working directory was given
        if working_dir == "" and self.window.active_view() and self.window.active_view().file_name():
            working_dir = os.path.dirname(self.window.active_view().file_name())

        if output_build_word_wrap is None: output_build_word_wrap = view_settings.get("output_build_word_wrap", False)
        if spell_check is None: spell_check = view_settings.get("build_view_spell_check", False)
        if gutter is None: gutter = view_settings.get("gutter", True)

        self.output_view.settings().set("result_file_regex", file_regex)
        self.output_view.settings().set("result_line_regex", line_regex)
        self.output_view.settings().set("result_base_dir", working_dir)
        self.output_view.settings().set("output_build_word_wrap", output_build_word_wrap)
        self.output_view.settings().set("spell_check", spell_check)
        self.output_view.settings().set("gutter", gutter)
        self.output_view.settings().set("line_numbers", False)
        self.output_view.settings().set("fold_buttons", False)
        self.output_view.settings().set("mini_diff", False)
        self.output_view.settings().set("scroll_past_end", False)
        self.output_view.assign_syntax(syntax)

        # Call create_output_panel a second time after assigning the above
        # settings, so that it'll be picked up as a result buffer
        self.window.create_output_panel("exec")

        self.encoding = encoding
        self.quiet = quiet

        self.proc = None
        if not self.quiet:
            if shell_cmd:
                print("Running " + shell_cmd)
            elif cmd:
                cmd_string = cmd
                if not isinstance(cmd, str):
                    cmd_string = " ".join(cmd)
                print("Running " + cmd_string)

            # https://forum.sublimetext.com/t/how-to-keep-showing-building-on-the-status-bar/43965
            ThreadProgress("Building...", "Successfully Build the Project!")

        show_panel_on_build = view_settings.get("show_panel_on_build", True)
        if show_panel_on_build:
            self.window.run_command("show_panel", {"panel": "output.exec"})

        self.hide_phantoms()
        self.show_errors_inline = sublime.load_settings("Preferences.sublime-settings").get("show_errors_inline", True)

        merged_env = env.copy()
        if self.window.active_view():
            user_env = self.window.active_view().settings().get('build_env')
            if user_env:
                merged_env.update(user_env)

        # Change to the working dir, rather than spawning the process with it,
        # so that emitted working dir relative path names make sense
        if working_dir != "":
            os.chdir(working_dir)

        self.debug_text = ""
        if shell_cmd:
            self.debug_text += "[shell_cmd: " + shell_cmd + "]\n"
        else:
            self.debug_text += "[cmd: " + str(cmd) + "]\n"
        self.debug_text += "[dir: " + str(os.getcwd()) + "]\n"
        if "PATH" in merged_env:
            self.debug_text += "[path: " + str(merged_env["PATH"]) + "]"
        else:
            self.debug_text += "[path: " + str(os.environ["PATH"]) + "]"

        try:
            # Forward kwargs to AsyncProcess
            self.proc = AsyncProcess(cmd, shell_cmd, merged_env, self, **kwargs)

            with self.text_queue_lock:
                self.text_queue_proc = self.proc

        except Exception as e:
            ThreadProgress.stop()
            self.append_string(None, str(e) + "\n")
            self.append_string(None, self.debug_text + "\n")
            if not self.quiet:
                self.append_string(None, "[Finished]")

    def is_enabled(self, kill=False, **kwargs):
        if kill:
            return (self.proc is not None) and self.proc.poll()
        else:
            return True

    def append_string(self, proc, str):
        was_empty = False
        with self.text_queue_lock:
            if proc != self.text_queue_proc and proc:
                # a second call to exec has been made before the first one
                # finished, ignore it instead of intermingling the output.
                proc.kill()
                return

            if len(self.text_queue) == 0:
                was_empty = True
                self.text_queue.append("")

            available = self.BLOCK_SIZE - len(self.text_queue[-1])

            if len(str) < available:
                cur = self.text_queue.pop()
                self.text_queue.append(cur + str)
            else:
                self.text_queue.append(str)

        if was_empty:
            sublime.set_timeout(self.service_text_queue, 0)

    def service_text_queue(self):
        is_empty = False
        with self.text_queue_lock:
            if len(self.text_queue) == 0:
                # this can happen if a new build was started, which will clear
                # the text_queue
                return

            characters = self.text_queue.popleft()
            is_empty = (len(self.text_queue) == 0)

        self.output_view.run_command(
            'append',
            {'characters': characters, 'force': True, 'scroll_to_end': True})

        if self.show_errors_inline and characters.find('\n') >= 0:
            errs = self.output_view.find_all_results_with_text()
            errs_by_file = {}
            for file, line, column, text in errs:
                if file not in errs_by_file:
                    errs_by_file[file] = []
                errs_by_file[file].append((line, column, text))
            self.errs_by_file = errs_by_file

            self.update_phantoms()

        if not is_empty:
            sublime.set_timeout(self.service_text_queue, 1)

    def finish(self, proc):
        if not self.quiet:
            elapsed = time.time() - proc.start_time
            exit_code = proc.exit_code()
            if exit_code == 0 or exit_code is None:
                self.append_string(proc, "[Finished in %.1fs]" % elapsed)
            else:
                self.append_string(proc, "[Finished in %.1fs with exit code %d]\n" % (elapsed, exit_code))
                self.append_string(proc, self.debug_text)

        if proc != self.proc:
            return

        ThreadProgress.stop()
        errs = self.output_view.find_all_results()

        if len(errs) == 0:
            sublime.status_message("Build finished")
        else:
            sublime.status_message("Build finished with %d errors" % len(errs))

        self.restoreViewPositions()

    def restoreViewPositions(self):
        window_id = self.window.id()
        restoring_scroll = False

        if window_id in g_last_scroll_positions:
            last_scroll_region, last_caret_region = g_last_scroll_positions[window_id]

            if last_scroll_region:
                restoring_scroll = True
                output_view = self.output_view

                # print('After  substr:                     ', output_view.substr(sublime.Region(0, 10)))
                # print('After  window.id:                  ', self.window.id())
                # print('After  output_view:                ', output_view)
                # print('After  output_view.id:             ', output_view.id())
                # print('g_last_scroll_positions[window_id] ', g_last_scroll_positions[window_id])

                def delayed_restore():
                    output_view.set_viewport_position(last_scroll_region)
                    output_view.sel().clear()

                    for selection in last_caret_region:
                        output_view.sel().add(sublime.Region(selection[0], selection[1]))

                sublime.set_timeout(delayed_restore, 0)

        if not restoring_scroll:
            sublime.set_timeout(self.fix_line_wrap_bug, 0)

    def fix_line_wrap_bug(self):
        """
            The output build panel is completely scrolled horizontally to the right when there are build errors
            https://github.com/SublimeTextIssues/Core/issues/2239
        """
        window = self.window
        output_view = window.find_output_panel( "exec" )

        viewport_position = output_view.viewport_position()
        output_view.set_viewport_position((0, viewport_position[1]))

    def on_data(self, proc, data):
        # Normalize newlines, Sublime Text always uses a single \n separator
        # in memory.
        data = data.replace('\r\n', '\n').replace('\r', '\n')

        self.append_string(proc, data)

    def on_finished(self, proc):
        sublime.set_timeout(functools.partial(self.finish, proc), 0)

    def update_phantoms(self):
        stylesheet = '''
            <style>
                div.error-arrow {
                    border-top: 0.4rem solid transparent;
                    border-left: 0.5rem solid color(var(--redish) blend(var(--background) 30%));
                    width: 0;
                    height: 0;
                }
                div.error {
                    padding: 0.4rem 0 0.4rem 0.7rem;
                    margin: 0 0 0.2rem;
                    border-radius: 0 0.2rem 0.2rem 0.2rem;
                }

                div.error span.message {
                    padding-right: 0.7rem;
                }

                div.error a {
                    text-decoration: inherit;
                    padding: 0.35rem 0.7rem 0.45rem 0.8rem;
                    position: relative;
                    bottom: 0.05rem;
                    border-radius: 0 0.2rem 0.2rem 0;
                    font-weight: bold;
                }
                html.dark div.error a {
                    background-color: #00000018;
                }
                html.light div.error a {
                    background-color: #ffffff18;
                }
            </style>
        '''

        for file, errs in self.errs_by_file.items():
            view = self.window.find_open_file(file)
            if view:

                buffer_id = view.buffer_id()
                if buffer_id not in self.phantom_sets_by_buffer:
                    phantom_set = sublime.PhantomSet(view, "exec")
                    self.phantom_sets_by_buffer[buffer_id] = phantom_set
                else:
                    phantom_set = self.phantom_sets_by_buffer[buffer_id]

                phantoms = []

                for line, column, text in errs:
                    pt = view.text_point(line - 1, column - 1)
                    phantoms.append(sublime.Phantom(
                        sublime.Region(pt, view.line(pt).b),
                        ('<body id=inline-error>' + stylesheet +
                            '<div class="error-arrow"></div><div class="error">' +
                            '<span class="message">' + html.escape(text, quote=False) + '</span>' +
                            '<a href=hide>' + chr(0x00D7) + '</a></div>' +
                            '</body>'),
                        sublime.LAYOUT_BELOW,
                        on_navigate=self.on_phantom_navigate))

                phantom_set.update(phantoms)

    def hide_phantoms(self):
        for file, errs in self.errs_by_file.items():
            view = self.window.find_open_file(file)
            if view:
                view.erase_phantoms("exec")

        self.errs_by_file = {}
        self.phantom_sets_by_buffer = {}
        self.show_errors_inline = False

    def on_phantom_navigate(self, url):
        self.hide_phantoms()


class ExecEventListener(sublime_plugin.EventListener):
    def on_load(self, view):
        w = view.window()
        if w is not None:
            w.run_command('exec', {'update_phantoms_only': True})

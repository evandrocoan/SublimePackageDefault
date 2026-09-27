

import os

import sublime
import sublime_plugin


def package_exists(file_name):
    loose_packages_path = os.path.join(sublime.packages_path(), file_name.replace('.sublime-package', ''))
    installed_packages_path = os.path.join(sublime.installed_packages_path(), file_name)

    return os.path.exists(installed_packages_path) or os.path.exists(loose_packages_path)


class InstallPackageControlExtendedCommand(sublime_plugin.ApplicationCommand):
    filename = 'Package Control.sublime-package'
    manager_filename = 'PackagesManager.sublime-package'

    def run(self):
        sublime.run_command( "install_package_control" )

    def is_visible(self):
        package_control = not package_exists( self.filename )
        packages_manager = not package_exists( self.manager_filename )
        return package_control and packages_manager


class EnablePackageControlExtendedCommand(sublime_plugin.ApplicationCommand):
    preferences_file = 'Preferences.sublime-settings'

    def installed_package_name(self):
        if package_exists(InstallPackageControlExtendedCommand.filename):
            return 'Package Control'
        if package_exists(InstallPackageControlExtendedCommand.manager_filename):
            return 'PackagesManager'
        return None

    def is_visible(self):
        package_name = self.installed_package_name()
        if package_name is None:
            return False
        settings = sublime.load_settings(self.preferences_file)
        return package_name in settings.get('ignored_packages', [])

    def run(self):
        package_name = self.installed_package_name()
        if package_name is None:
            return
        settings = sublime.load_settings(self.preferences_file)
        ignored_packages = settings.get('ignored_packages', [])
        if package_name not in ignored_packages:
            return
        if package_name == 'Package Control':
            sublime.run_command('enable_package_control')
            return
        settings.set('ignored_packages', [
            package for package in ignored_packages if package != package_name
        ])
        sublime.save_settings(self.preferences_file)


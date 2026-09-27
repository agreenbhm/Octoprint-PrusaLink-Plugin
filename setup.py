import setuptools

plugin_identifier = "prusalink"
plugin_package = "octoprint_prusalink"
plugin_name = "OctoPrint-PrusaLink"
plugin_version = "0.1.0"
plugin_description = (
    "Connect OctoPrint to Prusa printers over the local PrusaLink HTTP API "
    "instead of a USB serial connection"
)
plugin_author = "agreenbhm"
plugin_url = "https://github.com/agreenbhm/Octoprint-PrusaLink-Plugin"
plugin_license = "AGPLv3"
plugin_requires = ["requests"]

try:
    import octoprint_setuptools
except ImportError:
    print(
        "Could not import OctoPrint's setuptools, are you sure you are running that "
        "under the same python installation that OctoPrint is installed under?"
    )
    import sys

    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier=plugin_identifier,
    package=plugin_package,
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    mail="",
    url=plugin_url,
    license=plugin_license,
    requires=plugin_requires,
)

setuptools.setup(**setup_parameters)

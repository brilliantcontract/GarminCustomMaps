"""Checks metadata.txt the way the QGIS plugin manager does"""
import configparser
import os
import sys
import unittest

from qgis.testing import start_app

QGIS_APP = start_app()

from qgis.core import QgsApplication

# The plugin manager's own modules live next to the QGIS resources
sys.path.append(os.path.join(QgsApplication.pkgDataPath(), 'python'))
from pyplugin_installer.version_compare import isCompatible, pyQgisVersion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MetadataTest(unittest.TestCase):

    def test_installable_in_this_qgis(self):
        metadata = configparser.ConfigParser()
        metadata.read(os.path.join(ROOT, 'metadata.txt'), encoding='utf-8')
        general = metadata['general']
        minimum = general.get('qgisMinimumVersion', '0').strip()
        # Without a maximum, QGIS only accepts the minimum's major version (x.99)
        maximum = general.get('qgisMaximumVersion', '').strip() or minimum[0] + '.99'
        self.assertTrue(isCompatible(pyQgisVersion(), minimum, maximum),
                        f'QGIS {pyQgisVersion()} would reject a plugin for {minimum} - {maximum}')


if __name__ == '__main__':
    unittest.main()

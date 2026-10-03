"""End-to-end export test: runs the plugin in a headless QGIS and checks the kmz.

Run from the repository root with the Python that comes with QGIS:
    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest import mock

from qgis.testing import start_app

QGIS_APP = start_app()

from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsFillSymbol,
                       QgsGeometry, QgsProject, QgsRectangle, QgsVectorLayer)
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtCore import QCoreApplication, QSize
from qgis.PyQt.QtWidgets import QDialog, QMainWindow
from osgeo import gdal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KML = '{http://www.opengis.net/kml/2.2}'


def load_plugin():
    """Import the repository as the GarminCustomMap package (it uses relative imports)"""
    if 'GarminCustomMap' not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            'GarminCustomMap', os.path.join(ROOT, '__init__.py'),
            submodule_search_locations=[ROOT])
        package = importlib.util.module_from_spec(spec)
        sys.modules['GarminCustomMap'] = package
        spec.loader.exec_module(package)
    return importlib.import_module('GarminCustomMap.GarminCustomMap')


def wait_for_export(plugin, timeout=60):
    """Newer versions export in the background: wait until that finished"""
    deadline = time.time() + timeout
    while getattr(plugin, 'exporter', None) is not None:
        if time.time() > deadline:
            raise TimeoutError('the export did not finish')
        QCoreApplication.processEvents()
        time.sleep(0.01)


class FakeIface:
    """The parts of QgisInterface the plugin uses"""

    def __init__(self, canvas):
        self.main_window = QMainWindow()
        self.message_bar = QgsMessageBar()
        self.canvas = canvas
        self.status_bar = mock.Mock()

    def mainWindow(self):
        return self.main_window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.message_bar

    def statusBarIface(self):
        return self.status_bar

    def addToolBar(self, name):
        return self.main_window.addToolBar(name)

    def addPluginToMenu(self, menu, action):
        pass

    def removePluginMenu(self, menu, action):
        pass

    def removeToolBarIcon(self, action):
        pass


class ExportTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kmz_file = os.path.join(self.tmp.name, 'Test & map.kmz')

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()
        self.tmp.cleanup()

    def make_canvas(self, crs, extent, polygon_wkt):
        """A 400 x 300 canvas showing one filled polygon on a white background

        The extent must have the canvas' 4:3 shape, as a real canvas extent has.
        """
        layer = QgsVectorLayer('Polygon?crs=' + crs, 'area', 'memory')
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt(polygon_wkt))
        layer.dataProvider().addFeatures([feature])
        layer.renderer().setSymbol(QgsFillSymbol.createSimple(
            {'color': '30,120,200', 'outline_style': 'no'}))
        QgsProject.instance().addMapLayer(layer)

        canvas = QgsMapCanvas()
        canvas.resize(QSize(400, 300))
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem(crs))
        canvas.setLayers([layer])
        # The offscreen canvas does not resize itself, so set the output size directly
        settings = canvas.mapSettings()
        settings.setOutputSize(QSize(400, 300))
        canvas.mapSettings = lambda: settings
        canvas.extent = lambda: QgsRectangle(*extent)
        canvas.scale = lambda: 100000.0
        return canvas

    def export(self, canvas, **options):
        """Run the plugin as if the user picked kmz_file and pressed OK"""
        module = load_plugin()
        iface = FakeIface(canvas)
        # Keep the message bar texts, for checking warnings
        self.messages = []
        push_message = iface.message_bar.pushMessage
        iface.message_bar.pushMessage = lambda *args, **kwargs: (
            self.messages.append(args), push_message(*args, **kwargs))
        plugin = module.GarminCustomMap(iface)
        plugin.initGui()

        file_dialog = mock.Mock()
        file_dialog.exec.return_value = QDialog.DialogCode.Accepted
        file_dialog.selectedFiles.return_value = [self.kmz_file]

        def accept(dlg):
            dlg.zoom.setValue(options.get('zoom', 1.0))
            dlg.flag_optimize.setChecked(options.get('optimize', False))
            dlg.flag_skip_empty.setChecked(options.get('skip_empty', True))
            dlg.tile_width.setValue(options.get('tile_width', 256))
            dlg.tile_height.setValue(options.get('tile_height', 256))
            dlg.jpg_quality.setValue(95)
            if 'tile_limit' in options:
                dlg.tile_limit.setCurrentIndex((100, 500).index(options['tile_limit']))
            return 1

        # Older versions used the global iface from qgis.utils
        self.plugin = plugin
        with mock.patch.object(module, 'iface', iface, create=True), \
                mock.patch.object(module, 'QgsEncodingFileDialog', return_value=file_dialog), \
                mock.patch.object(module.GarminCustomMapDialog, 'exec', accept), \
                mock.patch.object(module.GarminCustomMapDialog, 'show'):
            plugin.run()
            if options.get('cancel'):
                plugin.exporter.cancel()
            wait_for_export(plugin)
        plugin.unload()
        if options.get('cancel'):
            return None
        self.assertTrue(os.path.exists(self.kmz_file), 'no kmz was written')
        return zipfile.ZipFile(self.kmz_file)

    def overlays(self, kmz):
        root = ET.fromstring(kmz.read('doc.kml'))
        result = []
        for overlay in root.iter(KML + 'GroundOverlay'):
            box = overlay.find(KML + 'LatLonBox')
            result.append({
                'href': overlay.find(KML + 'Icon/' + KML + 'href').text,
                **{side: float(box.find(KML + side).text)
                   for side in ('north', 'south', 'east', 'west')}})
        return root, result

    def read_tile(self, kmz, name):
        path = os.path.join(self.tmp.name, 'tile.jpg')
        with open(path, 'wb') as f:
            f.write(kmz.read(name))
        tile = gdal.Open(path)
        return [tile.GetRasterBand(b).ReadAsArray() for b in (1, 2, 3)]

    def assert_covers(self, overlays, extent):
        """The tiles together cover the (lon/lat) extent"""
        west, south, east, north = extent
        self.assertAlmostEqual(min(o['west'] for o in overlays), west, delta=0.01)
        self.assertAlmostEqual(max(o['east'] for o in overlays), east, delta=0.01)
        self.assertAlmostEqual(min(o['south'] for o in overlays), south, delta=0.01)
        self.assertAlmostEqual(max(o['north'] for o in overlays), north, delta=0.01)
        for o in overlays:
            self.assertLess(o['south'], o['north'])
            self.assertLess(o['west'], o['east'])

    def test_wgs84_export(self):
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))')
        kmz = self.export(canvas, zoom=2.0)
        root, overlays = self.overlays(kmz)

        # 800 x 600 pixels in 256 x 256 tiles: 4 columns, 3 rows
        self.assertEqual(len(overlays), 12)
        self.assertEqual(sorted(kmz.namelist()),
                         sorted(['doc.kml'] + [o['href'] for o in overlays]))
        self.assertEqual(root.find(KML + 'Document/' + KML + 'name').text.strip(), 'Test & map')
        self.assert_covers(overlays, (10, 59, 12, 60.5))
        for o in overlays:
            bands = self.read_tile(kmz, o['href'])
            self.assertLessEqual(bands[0].shape[0] * bands[0].shape[1], 1024 * 1024)

    def test_skip_empty_tiles(self):
        # The polygon only covers the left half of the map
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 10.9 59, 10.9 60.5, 10 60.5, 10 59))')
        kmz = self.export(canvas, zoom=2.0)
        _, overlays = self.overlays(kmz)
        self.assertEqual(len(overlays), 6)
        self.assertTrue(all(o['west'] < 11 for o in overlays))

    def test_reprojected_export(self):
        # UTM zone 32N around 10.5E 59.5N
        extent = (550000, 6590000, 590000, 6620000)
        canvas = self.make_canvas(
            'EPSG:32632', extent,
            'POLYGON((550000 6590000, 590000 6590000, 590000 6620000, 550000 6620000, 550000 6590000))')
        kmz = self.export(canvas, zoom=1.0)
        _, overlays = self.overlays(kmz)
        self.assertGreater(len(overlays), 0)
        for o in overlays:
            self.assertTrue(9.5 < o['west'] < o['east'] < 11.5, o)
            self.assertTrue(59 < o['south'] < o['north'] < 60, o)

    def test_optimized_tiles(self):
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))')
        kmz = self.export(canvas, zoom=4.0, optimize=True, skip_empty=False)
        _, overlays = self.overlays(kmz)
        # 1600 x 1200 pixels need at least two 1 megapixel tiles
        self.assertGreaterEqual(len(overlays), 2)
        self.assertLessEqual(len(overlays), 100)
        self.assert_covers(overlays, (10, 59, 12, 60.5))
        for o in overlays:
            bands = self.read_tile(kmz, o['href'])
            self.assertLessEqual(bands[0].size, 1024 * 1024)

    def test_full_colour(self):
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))')
        for layer in QgsProject.instance().mapLayers().values():
            layer.renderer().setSymbol(QgsFillSymbol.createSimple(
                {'color': '7,135,250', 'outline_style': 'no'}))
        kmz = self.export(canvas)
        _, overlays = self.overlays(kmz)
        bands = self.read_tile(kmz, overlays[0]['href'])
        # 5 bits per channel would give (0, 132, 255)
        for band, expected in zip(bands, (7, 135, 250)):
            self.assertAlmostEqual(int(band[100, 100]), expected, delta=2)

    def test_large_map_rendered_in_strips(self):
        # A diagonal edge shows whether the strips line up
        wkt = 'POLYGON((10 59, 12 59, 10 60.5, 10 59))'
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5), wkt)
        whole = self.export(canvas, zoom=2.0, skip_empty=False)
        whole_tiles = {o['href']: self.read_tile(whole, o['href']) for o in self.overlays(whole)[1]}
        module = load_plugin()
        os.remove(self.kmz_file)
        # 800 pixels wide, 37 rows per strip
        with mock.patch.object(module, 'MAX_STRIP_PIXELS', 800 * 37):
            canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5), wkt)
            strips = self.export(canvas, zoom=2.0, skip_empty=False)
        _, overlays = self.overlays(strips)
        self.assertEqual(sorted(o['href'] for o in overlays), sorted(whole_tiles))
        for o in overlays:
            for a, b in zip(self.read_tile(strips, o['href']), whole_tiles[o['href']]):
                self.assertLess(abs(a.astype(int) - b).mean(), 1, o['href'])

    def test_cancel_keeps_existing_file(self):
        with open(self.kmz_file, 'w') as f:
            f.write('old map')
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))')
        self.export(canvas, zoom=2.0, cancel=True)
        with open(self.kmz_file) as f:
            self.assertEqual(f.read(), 'old map')

    def tile_warnings(self):
        return [m for m in self.messages if 'exceeds the Garmin limit' in str(m)]

    def test_tile_limit(self):
        wkt = 'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))'
        # 1600 x 1200 pixels in 64 x 64 tiles: 25 x 19 = 475 tiles
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5), wkt)
        self.export(canvas, zoom=4.0, tile_width=64, tile_height=64, tile_limit=100)
        self.assertEqual(len(self.tile_warnings()), 1)
        self.assertIn('limit of 100 tiles', str(self.tile_warnings()[0]))
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5), wkt)
        self.export(canvas, zoom=4.0, tile_width=64, tile_height=64, tile_limit=500)
        self.assertEqual(self.tile_warnings(), [])

    def test_optimize_for_500_tiles(self):
        wkt = 'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))'
        # 12000 x 9000 = 108 megapixels can't fit into 100 tiles, but into 500
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5), wkt)
        kmz = self.export(canvas, zoom=30.0, optimize=True, skip_empty=False, tile_limit=500)
        _, overlays = self.overlays(kmz)
        self.assertLessEqual(len(overlays), 500)
        self.assertEqual(self.tile_warnings(), [])

    def test_zoom_below_one(self):
        canvas = self.make_canvas('EPSG:4326', (10, 59, 12, 60.5),
                                  'POLYGON((10 59, 12 59, 12 60.5, 10 60.5, 10 59))')
        kmz = self.export(canvas, zoom=0.5, tile_width=1024, tile_height=1024)
        _, overlays = self.overlays(kmz)
        # 200 x 150 pixels fit into one tile
        self.assertEqual(len(overlays), 1)
        self.assertEqual(self.read_tile(kmz, overlays[0]['href'])[0].shape, (150, 200))


if __name__ == '__main__':
    unittest.main()

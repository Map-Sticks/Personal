# -*- coding: utf-8 -*-
__persistentengine__ = True

import os
import sys

_bundle_dir = os.path.dirname(__file__)
_panel_dir = os.path.dirname(_bundle_dir)
_lib_dir = os.path.join(_panel_dir, "FixtureTools.lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

import fixture_placement_core as core
core.run(os.path.join(_bundle_dir, "ui.xaml"))

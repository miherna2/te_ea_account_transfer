"""ThousandEyes Enterprise Agent account-group migration tool."""
import os
import sys

__version__ = '0.1.0'

# No virtual environment: ``pip install --target libs -r requirements.txt`` puts the three
# libraries in the ``libs`` folder of the project. Use them when the folder exists.
_LIBS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'libs')
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

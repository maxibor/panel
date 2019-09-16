# coding: utf-8
"""
Defines a VTKPane which renders a vtk plot using VTKPlot bokeh model.
"""
from __future__ import absolute_import, division, unicode_literals

import sys
import os
import base64

try:
    from urllib.request import urlopen
except ImportError: # python 2
    from urllib import urlopen

from six import string_types
from functools import partial

import param

from pyviz_comms import JupyterComm

from ..base import PaneBase

if sys.version_info >= (2, 7):
    base64encode = lambda x: base64.b64encode(x).decode('utf-8')
else:
    base64encode = lambda x: x.encode('base64')


class VTK(PaneBase):
    """
    VTK panes allow rendering VTK objects.
    """

    camera = param.Dict(doc="""State of the rendered VTK camera.""")

    enable_keybindings = param.Boolean(default=False, doc="""
        Activate/Deactivate keys binding.

        Warning: These keys bind may not work as expected in a notebook
        context if they interact with already binded keys
    """)

    infer_legend = param.Boolean(default=False, doc="""In case of a vtkRenderWindow try to infer colorbar of actors""")

    _updates = True
    _serializers = {}

    @classmethod
    def applies(cls, obj):
        if (isinstance(obj, string_types) and obj.endswith('.vtkjs') or
            any([isinstance(obj, k) for k in cls._serializers.keys()])):
            return True
        elif 'vtk' not in sys.modules:
            return False
        else:
            import vtk
            return isinstance(obj, vtk.vtkRenderWindow)

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the bokeh model to be rendered.
        """
        if 'panel.models.vtk' not in sys.modules:
            if isinstance(comm, JupyterComm):
                self.param.warning('VTKPlot was not imported on instantiation '
                                   'and may not render in a notebook. Restart '
                                   'the notebook kernel and ensure you load '
                                   'it as part of the extension using:'
                                   '\n\npn.extension(\'vtk\')\n')
            from ...models.vtk import VTKPlot
        else:
            VTKPlot = getattr(sys.modules['panel.models.vtk'], 'VTKPlot')

        data = self._get_vtkjs()
        props = self._process_param_change(self._init_properties())
        vtkplot = VTKPlot(data=data, **props)
        if hasattr(self, '_legend') and self._legend:
            from bokeh.plotting import figure
            from bokeh.models import LinearColorMapper, ColorBar, Row
            ColorBars = [ColorBar(color_mapper=LinearColorMapper(low=v['low'], high=v['high'], palette=v['palette']), title=k) for k, v in self._legend.items()]
            sizing_mode = 'stretch_height' if vtkplot.sizing_mode in ['stretch_height', 'stretch_both'] else 'fixed'
            plot = figure(x_range=(0, 1), y_range=(0, 1), toolbar_location=None, width=110 * len(ColorBars),
                          sizing_mode=sizing_mode, height=vtkplot.height, min_height=vtkplot.min_height)

            plot.xaxis.visible = False
            plot.yaxis.visible = False
            plot.grid.visible = False
            plot.outline_line_alpha = 0
            [plot.add_layout(color_bar, 'right') for color_bar in ColorBars]
            model = Row(vtkplot, plot, sizing_mode=vtkplot.sizing_mode, height=vtkplot.height, width=vtkplot.width)
        else:
            model = vtkplot
        if root is None:
            root = model
        self._link_props(vtkplot, ['data', 'camera', 'enable_keybindings'], doc, root, comm)
        self._models[root.ref['id']] = (model, parent)
        return model

    def _init_properties(self):
        return {k: v for k, v in self.param.get_param_values()
                if v is not None and k not in ['default_layout', 'object', 'infer_legend']}

    @classmethod
    def register_serializer(cls, class_type, serializer):
        """
        Register a seriliazer for a given type of class.
        A serializer is a function which take an instance of `class_type`
        (like a vtk.vtkRenderWindow) as input and return the binary zip
        stream of the corresponding `vtkjs` file
        """
        cls._serializers.update({class_type:serializer})

    def _get_vtkjs(self):
        if self.object is None:
            vtkjs = None
        elif isinstance(self.object, string_types) and self.object.endswith('.vtkjs'):
            if os.path.isfile(self.object):
                with open(self.object, 'rb') as f:
                    vtkjs = f.read()
            else:
                data_url = urlopen(self.object)
                vtkjs = data_url.read()
        elif hasattr(self.object, 'read'):
            vtkjs = self.object.read()
        else:
            available_serializer = [v for k, v in VTK._serializers.items() if isinstance(self.object, k)]
            if len(available_serializer) == 0:
                import vtk
                from .vtkjs_serializer import render_window_serializer
                VTK.register_serializer(vtk.vtkRenderWindow, render_window_serializer)
                serializer = render_window_serializer
            else:
                serializer = available_serializer[0]

            try:
                from .vtkjs_serializer import render_window_serializer
                if serializer is render_window_serializer and self.infer_legend:
                    self._legend = {}
                    partial_serializer = partial(serializer, legend=self._legend)
                else:
                    partial_serializer = serializer
            except ImportError:
                partial_serializer = serializer
            vtkjs = partial_serializer(self.object)

        return base64encode(vtkjs) if vtkjs is not None else vtkjs

    def _update(self, model):
        model.data = self._get_vtkjs()


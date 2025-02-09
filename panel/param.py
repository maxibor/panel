"""
Defines the Param pane which converts Parameterized classes into a
set of widgets.
"""
from __future__ import absolute_import, division, unicode_literals

import os
import sys
import json
import types
import inspect
import itertools

from collections import OrderedDict, namedtuple
from six import string_types

import param

from bokeh.io import curdoc as _curdoc
from param.parameterized import classlist

from .io import state
from .layout import Row, Panel, Tabs, Column
from .links import Link
from .pane.base import Pane, PaneBase
from .util import (
    abbreviated_repr, full_groupby,
    get_method_owner, is_parameterized, param_name)
from .viewable import Layoutable, Reactive
from .widgets import (
    LiteralInput, Select, Checkbox, FloatSlider, IntSlider, RangeSlider,
    MultiSelect, StaticText, Button, Toggle, TextInput, DatetimeInput,
    DateRangeSlider, ColorPicker, Widget)
from .widgets.button import _ButtonBase


def FileSelector(pobj):
    """
    Determines whether to use a TextInput or Select widget for FileSelector
    """
    if pobj.path:
        return Select
    else:
        return TextInput


class Param(PaneBase):
    """
    Param panes render a Parameterized class to a set of widgets which
    are linked to the parameter values on the class.
    """

    display_threshold = param.Number(default=0, precedence=-10, doc="""
        Parameters with precedence below this value are not displayed.""")

    default_layout = param.ClassSelector(default=Column, class_=Panel,
                                         is_instance=False)

    default_precedence = param.Number(default=1e-8, precedence=-10, doc="""
        Precedence value to use for parameters with no declared
        precedence.  By default, zero predecence is available for
        forcing some parameters to the top of the list, and other
        values above the default_precedence values can be used to sort
        or group parameters arbitrarily.""")

    expand = param.Boolean(default=False, doc="""
        Whether parameterized subobjects are expanded or collapsed on
        instantiation.""")

    expand_button = param.Boolean(default=None, doc="""
        Whether to add buttons to expand and collapse sub-objects.""")

    expand_layout = param.Parameter(default=Column, doc="""
        Layout to expand sub-objects into.""")

    height = param.Integer(default=None, bounds=(0, None), doc="""
        Height of widgetbox the parameter widgets are displayed in.""")

    initializer = param.Callable(default=None, doc="""
        User-supplied function that will be called on initialization,
        usually to update the default Parameter values of the
        underlying parameterized object.""")

    parameters = param.List(default=[], doc="""
        If set this serves as a whitelist of parameters to display on the supplied
        Parameterized object.""")

    show_labels = param.Boolean(default=True, doc="""
        Whether to show labels for each widget""")

    show_name = param.Boolean(default=True, doc="""
        Whether to show the parameterized object's name""")

    width = param.Integer(default=300, bounds=(0, None), doc="""
        Width of widgetbox the parameter widgets are displayed in.""")

    widgets = param.Dict(doc="""
        Dictionary of widget overrides, mapping from parameter name
        to widget class.""")

    priority = 0.1

    _unpack = True

    _mapping = {
        param.Action:         Button,
        param.Parameter:      LiteralInput,
        param.Color:          ColorPicker,
        param.Dict:           LiteralInput,
        param.Selector:       Select,
        param.ObjectSelector: Select,
        param.FileSelector:   FileSelector,
        param.Boolean:        Checkbox,
        param.Number:         FloatSlider,
        param.Integer:        IntSlider,
        param.Range:          RangeSlider,
        param.String:         TextInput,
        param.ListSelector:   MultiSelect,
        param.Date:           DatetimeInput,
        param.DateRange:      DateRangeSlider
    }

    _rerender_params = []

    def __init__(self, object=None, **params):
        if isinstance(object, param.Parameter):
            if not 'show_name' in params:
                params['show_name'] = False
            params['parameters'] = [object.name]
            object = object.owner
        if isinstance(object, param.parameterized.Parameters):
            object = object.cls if object.self is None else object.self
        if 'parameters' not in params and object is not None:
            params['parameters'] = [p for p in object.param if p != 'name']
        super(Param, self).__init__(object, **params)
        self._updating = False

        # Construct Layout
        kwargs = {p: v for p, v in self.get_param_values() if p in Layoutable.param}
        self._widget_box = self.default_layout(**kwargs)

        layout = self.expand_layout
        if isinstance(layout, Panel):
            self._expand_layout = layout
            self.layout = self._widget_box
        elif isinstance(self._widget_box, layout):
            self.layout = self._expand_layout = self._widget_box
        elif isinstance(layout, type) and issubclass(layout, Panel):
            self.layout = self._expand_layout = layout(self._widget_box, **kwargs)
        else:
            raise ValueError('expand_layout expected to be a panel.layout.Panel'
                             'type or instance, found %s type.' %
                             type(layout).__name__)
        self.param.watch(self._update_widgets, [
            'object', 'parameters', 'display_threshold', 'expand_button',
            'expand', 'expand_layout', 'widgets', 'show_labels', 'show_name'])
        self._update_widgets()

    def __repr__(self, depth=0):
        cls = type(self).__name__
        obj_cls = type(self.object).__name__
        params = [] if self.object is None else list(self.object.param)
        parameters = [k for k in params if k != 'name']
        params = []
        for p, v in sorted(self.get_param_values()):
            if v is self.param[p].default: continue
            elif v is None: continue
            elif isinstance(v, string_types) and v == '': continue
            elif p == 'object' or (p == 'name' and (v.startswith(obj_cls) or v.startswith(cls))): continue
            elif p == 'parameters' and v == parameters: continue
            try:
                params.append('%s=%s' % (p, abbreviated_repr(v)))
            except RuntimeError:
                params.append('%s=%s' % (p, '...'))
        obj = type(self.object).__name__
        template = '{cls}({obj}, {params})' if params else '{cls}({obj})'
        return template.format(cls=cls, params=', '.join(params), obj=obj)

    #----------------------------------------------------------------
    # Callback API
    #----------------------------------------------------------------

    def _synced_params(self):
        ignored_params = ['name', 'default_layout']
        return [p for p in Layoutable.param if p not in ignored_params]

    def _update_widgets(self, *events):
        parameters = []
        for event in sorted(events, key=lambda x: x.name):
            if event.name == 'object':
                if isinstance(event.new, param.parameterized.Parameters):
                    self.object = object.cls if object.self is None else object.self
                    return
                if event.new is None:
                    parameters = None
                else:
                    parameters = [p for p in event.new.param if p != 'name']
            if event.name == 'parameters':
                parameters = None if event.new == [] else event.new

        if parameters != [] and parameters != self.parameters:
            self.parameters = parameters
            return

        for cb in list(self._callbacks):
            if cb.inst in self._widget_box.objects:
                cb.inst.param.unwatch(cb)
                self._callbacks.remove(cb)

        # Construct widgets
        if self.object is None:
            self._widgets = {}
        else:
            self._widgets = self._get_widgets()
        widgets = [widget for p, widget in self._widgets.items()
                   if (self.object.param[p].precedence is None)
                   or (self.object.param[p].precedence >= self.display_threshold)]
        self._widget_box.objects = widgets
        if not (self.expand_button == False and not self.expand):
            self._link_subobjects()

    def _link_subobjects(self):
        for pname, widget in self._widgets.items():
            widgets = [widget] if isinstance(widget, Widget) else widget
            if not any(is_parameterized(getattr(w, 'value', None)) or
                       any(is_parameterized(o) for o in getattr(w, 'options', []))
                       for w in widgets):
                continue
            if (isinstance(widgets, Row) and isinstance(widgets[1], Toggle)):
                selector, toggle = (widgets[0], widgets[1])
            else:
                selector, toggle = (widget, None)

            def toggle_pane(change, parameter=pname):
                "Adds or removes subpanel from layout"
                parameterized = getattr(self.object, parameter)
                existing = [p for p in self._expand_layout.objects
                            if isinstance(p, Param)
                            and p.object is parameterized]
                if existing:
                    old_panel = existing[0]
                    if not change.new:
                        self._expand_layout.remove(old_panel)
                elif change.new:
                    kwargs = {k: v for k, v in self.get_param_values()
                              if k not in ['name', 'object', 'parameters']}
                    pane = Param(parameterized, name=parameterized.name,
                                 **kwargs)
                    if isinstance(self._expand_layout, Tabs):
                        title = self.object.param[pname].label
                        pane = (title, pane)
                    self._expand_layout.append(pane)

            def update_pane(change, parameter=pname):
                "Adds or removes subpanel from layout"
                layout = self._expand_layout
                existing = [p for p in layout.objects if isinstance(p, Param)
                            and p.object is change.old]

                if toggle:
                    toggle.disabled = not is_parameterized(change.new)
                if not existing:
                    return
                elif is_parameterized(change.new):
                    parameterized = change.new
                    kwargs = {k: v for k, v in self.get_param_values()
                              if k not in ['name', 'object', 'parameters']}
                    pane = Param(parameterized, name=parameterized.name,
                                 **kwargs)
                    layout[layout.objects.index(existing[0])] = pane
                else:
                    layout.pop(existing[0])

            watchers = [selector.param.watch(update_pane, 'value')]
            if toggle:
                watchers.append(toggle.param.watch(toggle_pane, 'value'))
            self._callbacks += watchers

            if self.expand:
                if self.expand_button:
                    toggle.value = True
                else:
                    toggle_pane(namedtuple('Change', 'new')(True))

    def widget(self, p_name):
        """Get widget for param_name"""
        p_obj = self.object.param[p_name]
        kw_widget = {}

        if self.widgets is None or p_name not in self.widgets:
            widget_class = self.widget_type(p_obj)
        elif isinstance(self.widgets[p_name], dict):
            if 'type' in self.widgets[p_name]:
                widget_class = self.widgets[p_name].pop('type')
            else:
                widget_class = self.widget_type(p_obj)
            kw_widget = self.widgets[p_name]
        else:
            widget_class = self.widgets[p_name]
        value = getattr(self.object, p_name)

        if not self.show_labels and not issubclass(widget_class, _ButtonBase):
            label = ''
        else:
            label = p_obj.label
        kw = dict(value=value, disabled=p_obj.constant, name=label)
        
        # Update kwargs
        kw.update(kw_widget)
        
        if hasattr(p_obj, 'get_range'):
            options = p_obj.get_range()
            if not options and value is not None:
                options = [value]
            kw['options'] = options
        if hasattr(p_obj, 'get_soft_bounds'):
            bounds = p_obj.get_soft_bounds()
            if bounds[0] is not None:
                kw['start'] = bounds[0]
            if bounds[1] is not None:
                kw['end'] = bounds[1]
            if ('start' not in kw or 'end' not in kw) and not issubclass(widget_class, LiteralInput):
                widget_class = LiteralInput
            if hasattr(widget_class, 'step') and getattr(p_obj, 'step', None):
                kw['step'] = p_obj.step

        kwargs = {k: v for k, v in kw.items() if k in widget_class.param}
        
        if isinstance(widget_class, Widget):
            widget = widget_class
        else:
            widget = widget_class(**kwargs)

        watchers = self._callbacks
        if isinstance(widget, Toggle):
            pass
        else:
            def link_widget(change):
                if self._updating:
                    return
                try:
                    self._updating = True
                    self.object.set_param(**{p_name: change.new})
                finally:
                    self._updating = False

            if isinstance(p_obj, param.Action):
                def action(change):
                    value(self.object)
                watcher = widget.param.watch(action, 'clicks')
            else:
                watcher = widget.param.watch(link_widget, 'value')

            def link(change, watchers=[watcher]):
                updates = {}
                if change.what == 'constant':
                    updates['disabled'] = change.new
                elif change.what == 'precedence':
                    if (change.new < self.display_threshold and
                        widget in self._widget_box.objects):
                        self._widget_box.pop(widget)
                    elif change.new >= self.display_threshold:
                        precedence = lambda k: self.object.param[k].precedence
                        params = self._ordered_params
                        if self.show_name:
                            params.insert(0, 'name')
                        widgets = []
                        for k in params:
                            if precedence(k) is None or precedence(k) >= self.display_threshold:
                                widgets.append(self._widgets[k])
                        self._widget_box.objects = widgets
                    return
                elif change.what == 'objects':
                    updates['options'] = p_obj.get_range()
                elif change.what == 'bounds':
                    start, end = p_obj.get_soft_bounds()
                    updates['start'] = start
                    updates['end'] = end
                elif change.what == 'step':
                    updates['step'] = p_obj.step
                elif change.what == 'label':
                    updates['name'] = p_obj.label
                elif self._updating:
                    return
                elif isinstance(p_obj, param.Action):
                    widget.param.unwatch(watchers[0])
                    def action(event):
                        change.new(self.object)
                    watchers[0] = widget.param.watch(action, 'clicks')
                    return
                else:
                    updates['value'] = change.new

                try:
                    self._updating = True
                    widget.set_param(**updates)
                finally:
                    self._updating = False

            # Set up links to parameterized object
            watchers.append(self.object.param.watch(link, p_name, 'constant'))
            watchers.append(self.object.param.watch(link, p_name, 'precedence'))
            watchers.append(self.object.param.watch(link, p_name, 'label'))
            if hasattr(p_obj, 'get_range'):
                watchers.append(self.object.param.watch(link, p_name, 'objects'))
            if hasattr(p_obj, 'get_soft_bounds'):
                watchers.append(self.object.param.watch(link, p_name, 'bounds'))
            if 'step' in kw:
                watchers.append(self.object.param.watch(link, p_name, 'step'))
            watchers.append(self.object.param.watch(link, p_name))

        options = kwargs.get('options', [])
        if isinstance(options, dict):
            options = options.values()
        if ((is_parameterized(value) or any(is_parameterized(o) for o in options))
            and (self.expand_button or (self.expand_button is None and not self.expand))):
            widget.margin = (5, 0, 5, 10)
            toggle = Toggle(name='\u22EE', button_type='primary',
                            disabled=not is_parameterized(value), max_height=30,
                            max_width=20, height_policy='fit', align='center',
                            margin=(0, 0, 0, 10))
            widget.width = self._widget_box.width-60
            return Row(widget, toggle, width_policy='max', margin=0)
        else:
            return widget

    @property
    def _ordered_params(self):
        params = [(p, pobj) for p, pobj in self.object.param.objects('existing').items()
                  if p in self.parameters or p == 'name']
        key_fn = lambda x: x[1].precedence if x[1].precedence is not None else self.default_precedence
        sorted_precedence = sorted(params, key=key_fn)
        filtered = [(k, p) for k, p in sorted_precedence]
        groups = itertools.groupby(filtered, key=key_fn)
        # Params preserve definition order in Python 3.6+
        dict_ordered_py3 = (sys.version_info.major == 3 and sys.version_info.minor >= 6)
        dict_ordered = dict_ordered_py3 or (sys.version_info.major > 3)
        ordered_groups = [list(grp) if dict_ordered else sorted(grp) for (_, grp) in groups]
        ordered_params = [el[0] for group in ordered_groups for el in group if el[0] != 'name']
        return ordered_params

    #----------------------------------------------------------------
    # Model API
    #----------------------------------------------------------------

    def _get_widgets(self):
        """Return name,widget boxes for all parameters (i.e., a property sheet)"""
        # Format name specially
        if self.expand_layout is Tabs:
            widgets = []
        elif self.show_name:
            name = param_name(self.object.name)
            widgets = [('name', StaticText(value='<b>{0}</b>'.format(name)))]
        else:
            widgets = []
        widgets += [(pname, self.widget(pname)) for pname in self._ordered_params]
        return OrderedDict(widgets)

    def _get_model(self, doc, root=None, parent=None, comm=None):
        model = self.layout._get_model(doc, root, parent, comm)
        self._models[root.ref['id']] = (model, parent)
        return model

    def _cleanup(self, root):
        self.layout._cleanup(root)
        super(Param, self)._cleanup(root)

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    @classmethod
    def applies(cls, obj):
        return (is_parameterized(obj) or
                isinstance(obj, param.parameterized.Parameters) or
                (isinstance(obj, param.Parameter) and obj.owner is not None))

    @classmethod
    def widget_type(cls, pobj):
        ptype = type(pobj)
        for t in classlist(ptype)[::-1]:
            if t in cls._mapping:
                if isinstance(cls._mapping[t], types.FunctionType):
                    return cls._mapping[t](pobj)
                return cls._mapping[t]

    def get_root(self, doc=None, comm=None):
        """
        Returns the root model and applies pre-processing hooks

        Arguments
        ---------
        doc: bokeh.Document
          Bokeh document the bokeh model will be attached to.
        comm: pyviz_comms.Comm
          Optional pyviz_comms when working in notebook

        Returns
        -------
        Returns the bokeh model corresponding to this panel object
        """
        doc = doc or _curdoc()
        root = self.layout.get_root(doc, comm)
        ref = root.ref['id']
        self._models[ref] = (root, None)
        state._views[ref] = (self, root, doc, comm)
        return root


class ParamMethod(PaneBase):
    """
    ParamMethod panes wrap methods on parameterized classes and
    rerenders the plot when any of the method's parameters change. By
    default ParamMethod will watch all parameters on the class owning
    the method or can be restricted to certain parameters by annotating
    the method using the param.depends decorator. The method may
    return any object which itself can be rendered as a Pane.
    """

    def __init__(self, object, **params):
        self._kwargs =  {p: params.pop(p) for p in list(params)
                         if p not in self.param}
        super(ParamMethod, self).__init__(object, **params)
        kwargs = dict(self.get_param_values(), **self._kwargs)
        del kwargs['object']
        self._pane = Pane(self._eval_function(self.object), **kwargs)
        self._inner_layout = Row(self._pane, **{k: v for k, v in params.items() if k in Row.param})
        self._link_object_params()

    #----------------------------------------------------------------
    # Callback API
    #----------------------------------------------------------------

    @classmethod
    def _eval_function(self, function):
        args, kwargs = (), {}
        if hasattr(function, '_dinfo'):
            arg_deps = function._dinfo['dependencies']
            kw_deps = function._dinfo.get('kw', {})
            if kw_deps or any(isinstance(d, param.Parameter) for d in arg_deps):
                args = (getattr(dep.owner, dep.name) for dep in arg_deps)
                kwargs = {n: getattr(dep.owner, dep.name) for n, dep in kw_deps.items()}
        return function(*args, **kwargs)

    def _update_pane(self, *args):
        new_object = self._eval_function(self.object)
        pane_type = self.get_pane_type(new_object)
        try:
            links = Link.registry.get(new_object)
        except TypeError:
            links = []
        if type(self._pane) is pane_type and not links:
            if isinstance(new_object, Reactive):
                pvals = dict(self._pane.get_param_values())
                new_params = {k: v for k, v in new_object.get_param_values()
                              if k != 'name' and v is not pvals[k]}
                self._pane.set_param(**new_params)
            else:
                self._pane.object = new_object
        else:
            # Replace pane entirely
            kwargs = dict(self.get_param_values(), **self._kwargs)
            del kwargs['object']
            self._pane = Pane(new_object, **kwargs)
            self._inner_layout[0] = self._pane

    def _link_object_params(self):
        parameterized = get_method_owner(self.object)
        params = parameterized.param.params_depended_on(self.object.__name__)
        deps = params

        def update_pane(*events):
            # Update nested dependencies if parameterized object events
            if any(is_parameterized(event.new) for event in events):
                new_deps = parameterized.param.params_depended_on(self.object.__name__)
                for p in list(deps):
                    if p in new_deps: continue
                    watchers = self._callbacks
                    for w in list(watchers):
                        if (w.inst is p.inst and w.cls is p.cls and
                            p.name in w.parameter_names):
                            obj = p.cls if p.inst is None else p.inst
                            obj.param.unwatch(w)
                            watchers.pop(watchers.index(w))
                    deps.pop(deps.index(p))

                new_deps = [dep for dep in new_deps if dep not in deps]
                for _, params in full_groupby(new_deps, lambda x: (x.inst or x.cls, x.what)):
                    p = params[0]
                    pobj = p.cls if p.inst is None else p.inst
                    ps = [_p.name for _p in params]
                    watcher = pobj.param.watch(update_pane, ps, p.what)
                    self._callbacks.append(watcher)
                    for p in params:
                        deps.append(p)
            self._update_pane()

        for _, params in full_groupby(params, lambda x: (x.inst or x.cls, x.what)):
            p = params[0]
            pobj = (p.inst or p.cls)
            ps = [_p.name for _p in params]
            watcher = pobj.param.watch(update_pane, ps, p.what)
            self._callbacks.append(watcher)

    #----------------------------------------------------------------
    # Model API
    #----------------------------------------------------------------

    def _get_model(self, doc, root=None, parent=None, comm=None):
        if root is None:
            return self.get_root(doc, comm)

        ref = root.ref['id']
        if ref in self._models:
            self._cleanup(root)
        model = self._inner_layout._get_model(doc, root, parent, comm)
        self._models[ref] = (model, parent)
        return model

    def select(self, selector=None):
        """
        Iterates over the Viewable and any potential children in the
        applying the Selector.

        Arguments
        ---------
        selector: type or callable or None
          The selector allows selecting a subset of Viewables by
          declaring a type or callable function to filter by.

        Returns
        -------
        viewables: list(Viewable)
        """
        selected = super(ParamMethod, self).select(selector)
        selected += self._pane.select(selector)
        return selected

    def _cleanup(self, root=None):
        self._inner_layout._cleanup(root)
        super(ParamMethod, self)._cleanup(root)

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    @classmethod
    def applies(cls, obj):
        return inspect.ismethod(obj) and isinstance(get_method_owner(obj), param.Parameterized)


class ParamFunction(ParamMethod):
    """
    ParamFunction panes wrap functions decorated with the param.depends
    decorator and rerenders the output when any of the function's
    dependencies change. This allows building reactive components into
    a Panel which depend on other parameters, e.g. tying the value of
    a widget to some other output.
    """

    priority = 0.6

    def _link_object_params(self):
        deps = self.object._dinfo
        dep_params = list(deps['dependencies']) + list(deps.get('kw', {}).values())
        for p in dep_params:
            watcher = p.owner.param.watch(self._update_pane, p.name)
            self._callbacks.append(watcher)

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    @classmethod
    def applies(cls, obj):
        return isinstance(obj, types.FunctionType) and hasattr(obj, '_dinfo')


class JSONInit(param.Parameterized):
    """
    Callable that can be passed to Widgets.initializer to set Parameter
    values using JSON. There are three approaches that may be used:
    1. If the json_file argument is specified, this takes precedence.
    2. The JSON file path can be specified via an environment variable.
    3. The JSON can be read directly from an environment variable.
    Here is an easy example of setting such an environment variable on
    the commandline:
    PARAM_JSON_INIT='{"p1":5}' jupyter notebook
    This addresses any JSONInit instances that are inspecting the
    default environment variable called PARAM_JSON_INIT, instructing it to set
    the 'p1' parameter to 5.
    """

    varname = param.String(default='PARAM_JSON_INIT', doc="""
        The name of the environment variable containing the JSON
        specification.""")

    target = param.String(default=None, doc="""
        Optional key in the JSON specification dictionary containing the
        desired parameter values.""")

    json_file = param.String(default=None, doc="""
        Optional path to a JSON file containing the parameter settings.""")

    def __call__(self, parameterized):
        warnobj = param.main if isinstance(parameterized, type) else parameterized
        param_class = (parameterized if isinstance(parameterized, type)
                       else parameterized.__class__)

        target = self.target if self.target is not None else param_class.__name__

        env_var = os.environ.get(self.varname, None)
        if env_var is None and self.json_file is None: return

        if self.json_file or env_var.endswith('.json'):
            try:
                fname = self.json_file if self.json_file else env_var
                spec = json.load(open(os.path.abspath(fname), 'r'))
            except:
                warnobj.warning('Could not load JSON file %r' % spec)
        else:
            spec = json.loads(env_var)

        if not isinstance(spec, dict):
            warnobj.warning('JSON parameter specification must be a dictionary.')
            return

        if target in spec:
            params = spec[target]
        else:
            params = spec

        for name, value in params.items():
           try:
               parameterized.set_param(**{name:value})
           except ValueError as e:
               warnobj.warning(str(e))

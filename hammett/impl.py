import os
import sys

import hammett


class RaisesContext(object):
    def __init__(self, expected_exception, match):
        self.expected_exception = expected_exception
        self.excinfo = None
        self.match = match

    def __enter__(self):
        self.excinfo = ExceptionInfo()
        return self.excinfo

    def __exit__(self, *tp):
        __tracebackhide__ = True
        if tp[0] is None:
            assert False, f'Did not raise {self.expected_exception}'
        if self.match:
            import re
            assert re.match(self.match, str(tp[1]))
        self.excinfo.value = tp[1]
        self.excinfo.type = tp[0]
        suppress_exception = issubclass(self.excinfo.type, self.expected_exception)
        return suppress_exception


class ExceptionInfo:
    def __str__(self):
        return f'<ExceptionInfo: type={self.type} value={self.value}'


fixtures = {}
auto_use_fixtures = set()
fixture_scope = {}


def fixture_function_name(f):
    r = f.__name__
    if r == '<lambda>':
        import inspect
        r = inspect.getsource(f)
    return r


# TODO: store args
def register_fixture(fixture, *args, autouse=False, scope='function'):
    if scope == 'class':
        # hammett does not support class based tests
        return
    assert scope != 'package', 'Package scope is not supported at this time'

    name = fixture_function_name(fixture)
    # pytest uses shadowing.. I don't like it but I guess we have to follow that?
    # assert name not in fixtures, 'A fixture with this name is already registered'
    if hammett._verbose and name in fixtures:
        hammett.print(f'{fixture} shadows {fixtures[name]}')
    if autouse:
        auto_use_fixtures.add(name)
    assert scope in ('function', 'class', 'module', 'package', 'session')
    fixture_scope[name] = scope
    fixtures[name] = fixture


from hammett import fixtures as built_in_fixtures
for fixture_name, fixture in built_in_fixtures.__dict__.items():
    if not fixture_name.startswith('_'):
        register_fixture(fixture)


def pick_keys(kwargs, params):
    return {
        k: v
        for k, v in kwargs.items()
        if k in params
    }


class FixturesUnresolvableException(Exception):
    pass


def params_of(f):
    import inspect
    return set(
        x.name
        for x in inspect.signature(f).parameters.values()
        if x.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    )


def _teardown_yield_fixture(fixturefunc, it):
    """Executes the teardown of a fixture function by advancing the iterator after the
    yield and ensure the iteration ends (if not it means there is more than one yield in the function)"""
    try:
        next(it)
    except StopIteration:
        pass
    else:
        hammett.print(f"yield_fixture {fixturefunc} function has more than one 'yield'")
        exit(1)


def call_fixture_func(fixturefunc, request, kwargs):
    # TODO: this fixture crashes for some reason, blacklist it for now
    if fixturefunc.__name__ == '_dj_autoclear_mailbox':
        return None
    existing_result = request.hammett_get_existing_result(fixture_function_name(fixturefunc))
    if existing_result is not hammett.MISSING:
        return existing_result

    import inspect
    import functools

    from hammett import Request
    Request.current_fixture_setup = fixture_function_name(fixturefunc)

    res = fixturefunc(**kwargs)

    request.hammett_add_fixture_result(res)

    yieldctx = inspect.isgenerator(res)
    if yieldctx:
        it = fixturefunc(**kwargs)
        res = next(it)
        finalizer = functools.partial(_teardown_yield_fixture, fixturefunc, it)
        request.addfinalizer(finalizer)

    Request.current_fixture_setup = None

    return res


def dependency_injection(f, fixtures, orig_kwargs, request):
    fixtures = fixtures.copy()
    f_params = params_of(f)
    params_by_name = {
        name: params_of(fixture)
        for name, fixture in fixtures.items()
        # prune the fixture list based on what f needs
        if name in f_params or name in auto_use_fixtures or name == 'request'
    }

    def add_fixture(kwargs, name, indent=0):
        if name in kwargs:
            return

        params = params_of(fixtures[name])
        for param in params:
            add_fixture(kwargs, param, indent+1)
        params_by_name[name] = params

    kwargs = {}
    while params_by_name:
        reduced = False
        for name, params in list(params_by_name.items()):
            # If we have a dependency that we have pruned, unprune it
            for param in params:
                if param not in kwargs:
                    assert param in fixtures
                    params_by_name[param] = params_of(fixtures[param])
                    break
            # If we can resolve this dependency fully
            if params.issubset(set(kwargs.keys())):
                assert name not in kwargs
                kwargs[name] = call_fixture_func(fixtures[name], request, pick_keys(kwargs, params))
                if request is not None:
                    request.fixturenames.add(name)
                    for x in request.additional_fixtures_wanted:
                        add_fixture(kwargs, x)
                reduced = True
                del params_by_name[name]
        if not reduced:
            raise FixturesUnresolvableException(f'Could not resolve fixtures any more, have {params_by_name} left.\nAvailable dependencies: {kwargs.keys()}')

    if request is not None:
        request.fixturenames = set(kwargs.keys())
    # prune again, to get rid of auto use fixtures that f doesn't take
    kwargs = {k: v for k, v in kwargs.items() if k in f_params}
    return f(**{**kwargs, **orig_kwargs})


def should_stop():
    return hammett._fail_fast and (hammett.results['failed'] or hammett.results['abort'])


def should_skip(_f):
    if not hasattr(_f, 'hammett_markers'):
        return False

    for marker in _f.hammett_markers:
        if marker.name == 'skip':
            return True

    return False


def analyze_assert(tb):
    # grab assert source line
    try:
        with open(tb.tb_frame.f_code.co_filename) as f:
            source = f.read().split('\n')
    except FileNotFoundError:
        try:
            with open(os.path.join(hammett._orig_cwd, tb.tb_frame.f_code.co_filename)) as f:
                source = f.read().split('\n')
        except FileNotFoundError:
            hammett.print(f'Failed to analyze assert statement: file not found. Most likely there was a change of current directory.')
            return

    line_no = tb.tb_frame.f_lineno - 1
    relevant_source = source[line_no]

    # if it spans multiple lines grab them all
    while not relevant_source.strip().startswith('assert '):
        line_no -= 1
        relevant_source = source[line_no] + '\n' + relevant_source

    import ast
    try:
        assert_statement = ast.parse(relevant_source.strip()).body[0]
    except SyntaxError:
        hammett.print('Failed to analyze assert statement (SyntaxError)')
        return

    # We only analyze further if it's a comparison
    if assert_statement.test.__class__.__name__ != 'Compare':
        return

    # ...and if the left side is a function call
    if assert_statement.test.left.__class__.__name__ != 'Call':
        return

    hammett.print()
    hammett.print('--- Assert components ---')
    from astunparse import unparse
    left = eval(unparse(assert_statement.test.left), tb.tb_frame.f_globals, tb.tb_frame.f_locals)
    hammett.print('left:')
    hammett.print(f'   {left!r}')
    right = eval(unparse(assert_statement.test.comparators), tb.tb_frame.f_globals, tb.tb_frame.f_locals)
    hammett.print('right:')
    hammett.print(f'   {right!r}')


def run_test(_name, _f, _module_request, **kwargs):
    if should_skip(_f):
        hammett.results['skipped'] += 1
        return

    import colorama
    from io import StringIO
    from hammett.impl import register_fixture

    req = hammett.Request(scope='function', parent=_module_request, function=_f)

    def request():
        return req

    register_fixture(request, autouse=True)
    del request

    hijacked_stdout = StringIO()
    hijacked_stderr = StringIO()

    if hammett._verbose:
        hammett.print(_name + '...', end='', flush=True)
    try:
        from hammett.impl import (
            dependency_injection,
            fixtures,
        )

        sys.stdout = hijacked_stdout
        sys.stderr = hijacked_stderr

        dependency_injection(_f, fixtures, kwargs, request=req)

        sys.stdout = hammett._orig_stdout
        sys.stderr = hammett._orig_stderr

        if hammett._verbose:
            hammett.print(f' {colorama.Fore.GREEN}Success{colorama.Style.RESET_ALL}')
        else:
            hammett.print(f'{colorama.Fore.GREEN}.{colorama.Style.RESET_ALL}', end='', flush=True)
        hammett.results['success'] += 1
    except KeyboardInterrupt:
        sys.stdout = hammett._orig_stdout
        sys.stderr = hammett._orig_stderr

        hammett.print()
        hammett.print('ABORTED')
        hammett.results['abort'] += 1
    except:
        sys.stdout = hammett._orig_stdout
        sys.stderr = hammett._orig_stderr

        hammett.print(colorama.Fore.RED)
        if not hammett._verbose:
            hammett.print()
        hammett.print('Failed:', _name)
        hammett.print()

        import traceback
        hammett.print(traceback.format_exc())

        hammett.print()
        if hijacked_stdout.getvalue():
            hammett.print(colorama.Fore.YELLOW)
            hammett.print('--- stdout ---')
            hammett.print(hijacked_stdout.getvalue())

        if hijacked_stderr.getvalue():
            hammett.print(colorama.Fore.RED)
            hammett.print('--- stderr ---')
            hammett.print(hijacked_stderr.getvalue())

        hammett.print(colorama.Style.RESET_ALL)

        if not hammett._quiet:
            import traceback
            type, value, tb = sys.exc_info()
            while tb.tb_next:
                tb = tb.tb_next

            hammett.print('--- Local variables ---')
            for k, v in tb.tb_frame.f_locals.items():
                hammett.print(f'{k}:')
                try:
                    hammett.print('   ', repr(v))
                except Exception as e:
                    hammett.print(f'   Error getting local variable repr: {e}')

            if type == AssertionError:
                analyze_assert(tb)

        if hammett._drop_into_debugger:
            try:
                import ipdb as pdb
            except ImportError:
                import pdb
            pdb.set_trace()

        hammett.results['failed'] += 1

    # Tests can change this which breaks everything. Reset!
    os.chdir(hammett._orig_cwd)
    req.teardown()


def execute_parametrize(_name, _f, _stack, _module_request, **kwargs):
    if not _stack:
        param_names = [f'{k}={v}' for k, v in kwargs.items()]
        _name = f'{_name}[{", ".join(param_names)}]'
        return run_test(_name, _f, _module_request=_module_request, **kwargs)

    names, param_list = _stack[0]
    names = [x.strip() for x in names.split(',')]
    for params in param_list:
        if not isinstance(params, (list, tuple)):
            params = [params]
        execute_parametrize(_name, _f, _stack[1:], _module_request, **{**dict(zip(names, params)), **kwargs})
        if should_stop():
            break


def execute_test_function(_name, _f, module_request):
    if getattr(_f, 'hammett_parametrize_stack', None):
        return execute_parametrize(_name, _f, _f.hammett_parametrize_stack, module_request)
    else:
        return run_test(_name, _f, module_request)


class FakePytestParser:
    def parse_known_args(self, *args):
        class Fake:
            pass
        s = Fake()
        s.itv = True
        s.ds = hammett.settings['django_settings_module']
        s.dc = None
        s.version = False
        s.help = False
        return s


class EarlyConfig:
    def __init__(self):
        self.inicfg = self
        self.config = self
        self.path = hammett._orig_cwd

    def addinivalue_line(self, name, doc):
        pass

    def getini(self, name):
        return None


early_config = EarlyConfig()


def load_plugin(module_name):
    import importlib
    try:
        plugin_module = importlib.import_module(module_name+'.plugin')
    except ImportError:
        plugin_module = importlib.import_module(module_name)

    parser = FakePytestParser()
    try:
        if hasattr(plugin_module, 'pytest_load_initial_conftests'):
            plugin_module.pytest_load_initial_conftests(early_config=early_config, parser=parser, args=[])

        if hasattr(plugin_module, 'pytest_configure'):
            import inspect
            if inspect.signature(plugin_module.pytest_configure).parameters:
                plugin_module.pytest_configure(early_config)
            else:
                plugin_module.pytest_configure()
    except Exception:
        hammett.print(f'Loading plugin {module_name} failed: ')
        import traceback
        hammett.print(traceback.format_exc())
        hammett.results['abort'] += 1
        return
    try:
        importlib.import_module(module_name + '.fixtures')
    except ImportError:
        pass
    return True


def read_settings():
    from configparser import (
        ConfigParser,
        NoSectionError,
    )
    config_parser = ConfigParser()
    config_parser.read('setup.cfg')
    try:
        hammett.settings.update(dict(config_parser.items('hammett')))
    except NoSectionError:
        return

    # load plugins
    if 'plugins' not in hammett.settings:
        return

    import importlib
    for plugin in hammett.settings['plugins'].strip().split('\n'):
        load_plugin(plugin)
        if should_stop():
            return

    try:
        conftest = importlib.import_module('conftest')
    except ImportError:
        conftest = None

    if conftest is not None:
        plugins = getattr(conftest, 'pytest_plugins', [])
        for plugin in plugins:
            load_plugin(plugin)
            if should_stop():
                return

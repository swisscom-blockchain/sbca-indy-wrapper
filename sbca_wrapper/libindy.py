import asyncio
import itertools
import json
import logging
import sys

from .error import IndyError, IndyErrorCode

from ctypes import CDLL, CFUNCTYPE, byref, c_char_p, c_int, c_void_p
from typing import Any, Callable, Dict, Tuple, Union

# Logger settings
_LOGGER = logging.getLogger('libindy')


class _Libindy:
    """
    Enables interactions with the installed C-library Libindy.
    """

    # Singleton instance
    _instance: '_Libindy' = None

    # Libindy instance fields
    _library: CDLL = None
    _futures: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
    _command_handle: itertools.count = itertools.count()

    # Libindy instance state fields
    initialized: bool = False

    # ------------------------------------------------------------------------------------------------------------------
    #  Constructor
    # ------------------------------------------------------------------------------------------------------------------
    def __new__(cls) -> '_Libindy':
        """
        Only create a new Libindy instance if none already exists

        :returns libindy: Libindy - Libindy instance
        """

        # Create new Libindy instance if none exists
        if not _Libindy._instance:
            _Libindy._instance = object.__new__(cls)
            _Libindy._library = _Libindy._load_library()
            _LOGGER.debug('Libindy library loaded.')
            pass

        # Return Libindy instance
        return _Libindy._instance

    @staticmethod
    def _load_library() -> CDLL:
        # Define supported OS platform keys
        lib_names = {'darwin': 'libindy.dylib', 'linux': 'libindy.so', 'linux2': 'libindy.so', 'win32': 'indy.dll'}

        # Load Libindy library
        try:
            return CDLL(lib_names.get(sys.platform))
        except KeyError:
            _LOGGER.error(f'Your OS is not supported by Libindy! >>> {sys.platform} not in {lib_names.keys()}\n')
            raise
        except OSError:
            _LOGGER.error(f'The Libindy library could not be loaded from your system! Is the library installed and the '
                          f'PATH variable set?\n')
            raise
        pass

    # ------------------------------------------------------------------------------------------------------------------
    #  Methods
    # ------------------------------------------------------------------------------------------------------------------

    # Libindy Commands -------------------------------------------------------------------------------------------------
    @staticmethod
    def run_command(command_name: str, *command_args) -> asyncio.Future:
        """
         Asynchronously run a command defined in the Libindy library.

        :param command_name: str - Libindy command name
        :param command_args - C-type encoded command arguments
        :returns command_future: Future - Future object for the command
        """

        # Raise error if Libindy is not initialized
        if not _Libindy.initialized:
            _LOGGER.error('Libindy has to be initialized before running commands!\n')
            raise RuntimeError

        # Get Libindy command from library
        command: Callable = _Libindy._get_command(command_name)

        # Create and store future
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        command_future: asyncio.Future = loop.create_future()
        command_handle: int = next(_Libindy._command_handle)
        _Libindy._futures[command_handle] = (loop, command_future)

        # Call Libindy library
        response_code: int = command(command_handle, *command_args)

        # Set exception
        if response_code != IndyErrorCode.Success:
            _LOGGER.error(f'Libindy library returned non-success code {response_code}!')
            command_future.set_exception(_Libindy._get_indy_error(response_code))
            pass

        # Return future object
        return command_future

    @staticmethod
    def sync_run_command(command_name: str, *command_args) -> Any:
        """
         Synchronously run a command defined in the Libindy library.

        :param command_name: str - Libindy command name
        :param command_args - C-type encoded command arguments
        :returns command_res: Any - Command response
        """
        return _Libindy._get_command(command_name)(*command_args)

    @staticmethod
    def implements_command(command_name: str) -> bool:
        """
        Get whether a specific command is implemented in the Libindy library installed on your system.

        :param command_name: str - Command name to check
        :returns is_installed: bool - Whether the command is installed
        """

        if not _Libindy._library:
            _LOGGER.error('Libindy library has not been loaded! Resolve this by creating a Libindy instance before '
                          'running indy commands.\n')
            raise RuntimeError
        return hasattr(_Libindy._library, command_name)

    @staticmethod
    def _get_command(command_name: str) -> Callable:
        if not _Libindy.implements_command(command_name):
            _LOGGER.error(f'Command {command_name} is not implemented in the Libindy library!')
            raise NotImplementedError
        return getattr(_Libindy._library, command_name)

    # Command Callback -------------------------------------------------------------------------------------------------
    @staticmethod
    def create_callback(c_callback_signature: CFUNCTYPE, transform_fn: Callable = None) -> Any:
        """
        Create a C-encoded callback function for a Libindy command.

        :param c_callback_signature: CFUNCTYPE - C-encoded callback function signature
        :param transform_fn: function <optional> - Function to alter response values before returning
        :returns callback_function: Any - C-encoded callback function
        """

        # Define Libindy callback function
        def _callback_fn(command_handle: int, response_code: int, *response_values) -> Any:
            # Transform callback return values (if necessary)
            response_values = transform_fn(*response_values) if transform_fn is not None else response_values
            response = _Libindy._get_indy_error(response_code)
            _Libindy._indy_callback(command_handle, response, *response_values)
            pass

        # Call Libindy callback function with C-types
        return c_callback_signature(_callback_fn)

    @staticmethod
    def _indy_callback(command_handle: int, response: IndyError, *response_values) -> None:
        loop, _ = _Libindy._futures[command_handle]
        loop.call_soon_threadsafe(_Libindy._indy_loop_callback, command_handle, response, *response_values)
        pass

    @staticmethod
    def _indy_loop_callback(command_handle: int, response: IndyError, *response_values) -> None:
        # Get command future from futures stack
        _, future = _Libindy._futures.pop(command_handle)

        # Set future result
        if not future.cancelled():

            # Build and return command response
            if response.code == IndyErrorCode.Success:
                future.set_result(None if not response_values else tuple(response_values))
                pass

            # Handle command exception
            else:
                _LOGGER.error(f'Libindy returned non-success code {response.code} ({response.name})!')
                future.set_exception(response)
                pass
            pass
        else:
            _LOGGER.debug('Command future was cancelled before callback execution')
            pass
        pass

    # Various ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _get_indy_error(code: int) -> IndyError:
        # Get indy code from response
        try:
            indy_code = IndyErrorCode(code)
            pass
        except KeyError:
            _LOGGER.error(f'Libindy command responded with unknown response code {code}!')
            raise

        # Return success
        if indy_code is IndyErrorCode.Success:
            return IndyError(IndyErrorCode.Success)

        # Get and return error data
        c_error = c_char_p()
        _Libindy._get_command('indy_get_current_error')(byref(c_error))
        error_details: dict = json.loads(c_error.value.decode())
        return IndyError(indy_code, error_details.get('message', indy_code.name), error_details.get('backtrace', None))


def initialize_libindy(runtime_config: Union[dict, str] = None) -> None:
    """
    Initialize Libindy by setting the library logger and optionally a runtime config.

    :param runtime_config: str, dict <optional> - Runtime configuration settings for the Libindy library
        -> Will only be called when first Libindy instance is created!
        {
            crypto_thread_pool_size: int <optional, default: 4> - Thread pool size for crypto operations
            collect_backtrace: bool <optional, default: ?> - Whether to collect backtrace of library errors
        }
    """

    # Raise error if Libindy is already initialized
    if _Libindy.initialized:
        _LOGGER.error('Libindy is already initialized!\n')
        raise RuntimeError

    _LOGGER.debug('Initializing Libindy...')

    # Set runtime config
    if runtime_config:
        runtime_config: str = runtime_config if isinstance(runtime_config, str) else json.dumps(runtime_config)
        _Libindy.sync_run_command('indy_set_runtime_config', c_char_p(runtime_config.encode('utf-8')))
        _LOGGER.debug('Libindy runtime config set.')
        pass

    # Add log level
    logging.addLevelName(5, 'TRACE')

    # Logging function
    def _log(context, level, target, message, module_path, file, line):
        libindy_logger = _LOGGER.getChild(f'native.{target.decode().replace("::", ".")}')
        level_mapping = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO, 4: logging.DEBUG, 5: 5, }
        libindy_logger.log(level_mapping[level], f'\t{file.decode()}:{line} | {message.decode()}')
        pass

    # Define logger callback functions
    initialize_libindy.callbacks = {
        'enabled_cb': None,
        'log_cb': CFUNCTYPE(None, c_void_p, c_int, c_char_p, c_char_p, c_char_p, c_char_p, c_int)(_log),
        'flush_cb': None
    }

    # Run indy command
    _Libindy.sync_run_command('indy_set_logger', None, initialize_libindy.callbacks['enabled_cb'],
                              initialize_libindy.callbacks['log_cb'], initialize_libindy.callbacks['flush_cb'])
    _LOGGER.debug('Libindy library logger set.')

    # Flag Libindy as initialized
    _Libindy.initialized = True
    _LOGGER.debug('Libindy initialized.')
    pass


# Return Libindy instance
Libindy = _Libindy()
from . import test_connector_transaction
from . import test_operations
from . import test_replay_safety
from . import test_security
from . import test_recovery

# Standalone client tests must not be imported here — they stub odoo via sys.modules.

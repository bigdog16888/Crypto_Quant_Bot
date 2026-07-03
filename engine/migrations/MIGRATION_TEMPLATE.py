from engine.migrations.migration_base import SafeMigration

class Migration00X(SafeMigration):
    # Set to True if this migration modifies existing bot_orders 
    # or trades rows. Set to False if only adding schema.
    requires_flat_positions = True  # REVIEW THIS BEFORE DEPLOYING
    
    @classmethod
    def _run_impl(cls, conn):
        # Migration SQL here
        pass

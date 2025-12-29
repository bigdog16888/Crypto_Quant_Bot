import streamlit as st
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from engine.database import get_all_bots, toggle_bot_active, delete_bot

def render_bot_manager_view():
    st.header("Bot Manager")
    st.caption("Manage existing bots: Toggle Status or Delete.")
    
    # Fetch Data
    bots = get_all_bots()
    
    if not bots:
        st.info("No bots found. Go to 'Bot Creator' to deploy one.")
        return
        
    st.markdown("### Active Inventory")
    
    # Header Row
    cols = st.columns([1, 2, 2, 2, 2, 2, 1])
    cols[0].markdown("**ID**")
    cols[1].markdown("**Name**")
    cols[2].markdown("**Pair**")
    cols[3].markdown("**Strategy**")
    cols[4].markdown("**Invested**")
    cols[5].markdown("**Status**")
    cols[6].markdown("**Action**")
    
    st.divider()

    for bot in bots:
        b_id, name, pair, is_active, strat_type, total_invested, step = bot
        
        # Display Row
        row_cols = st.columns([1, 2, 2, 2, 2, 2, 1])
        row_cols[0].write(f"#{b_id}")
        row_cols[1].write(name)
        row_cols[2].write(pair)
        row_cols[3].write(strat_type)
        row_cols[4].write(f"${total_invested:.2f} (Step {step})")
        
        # Toggle Status
        with row_cols[5]:
            status_label = "Running" if is_active else "Stopped"
            # Use columns to make toggle smaller/neater
            if st.toggle(status_label, value=bool(is_active), key=f"toggle_{b_id}") != bool(is_active):
                # State changed
                new_state = not bool(is_active)
                toggle_bot_active(b_id, new_state)
                st.rerun()
        
        # Delete Action
        with row_cols[6]:
            if st.button("🗑️", key=f"del_{b_id}", help=f"Delete {name}"):
                if delete_bot(b_id):
                    st.success(f"Deleted {name}")
                    st.rerun()
                else:
                    st.error("Delete failed")
        
        st.divider()

"""
Minuteur d'un fragment Streamlit réglé depuis le fragment lui-même (sans réexécuter la page).

`st.fragment(run_every=…)` fixe l'intervalle à l'appel du fragment, donc seulement lors d'une exécution complète
de la page. Ici, le fragment en cours (re)programme ou arrête son propre minuteur : suivi rapproché pendant une
tâche de fond, puis minuteur arrêté (ou espacé) à sa fin, sans st.rerun() de toute la page. Le navigateur efface
tous les minuteurs à chaque exécution complète : un fragment qui n'est plus affiché (autre page) n'en a plus.

Repose sur des messages internes de Streamlit (AutoRerun / StopAutoRerun) : si leur API change, set_timer renvoie
False et le minuteur posé à l'appel du fragment (run_every) reste en place.
"""


def _current_fragment():
    """(contexte d'exécution, identifiant du fragment en cours) ou (None, None) hors fragment."""
    from streamlit.runtime.scriptrunner_utils.script_run_context import ThreadState, get_script_run_ctx
    ctx = get_script_run_ctx(suppress_warning=True)
    fragment_id = ThreadState.get().fragment_id if ctx else None
    return (ctx, fragment_id) if fragment_id else (None, None)


def set_timer(seconds) -> bool:
    """
    Minuteur du fragment en cours : réexécution toutes les `seconds` secondes, ou arrêt si `seconds` est vide
    ou nul. Renvoie False hors fragment ou si Streamlit ne permet plus ce réglage.
    """
    try:
        from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
        ctx, fragment_id = _current_fragment()
        if not fragment_id:
            return False
        msg = ForwardMsg()
        if seconds and float(seconds) > 0:
            msg.auto_rerun.interval = float(seconds)
            msg.auto_rerun.fragment_id = fragment_id
        else:
            msg.stop_auto_rerun.fragment_ids.append(fragment_id)
        ctx.enqueue(msg)
        return True
    except Exception:   # noqa: BLE001 - API interne de Streamlit : on garde le minuteur de l'appel
        return False

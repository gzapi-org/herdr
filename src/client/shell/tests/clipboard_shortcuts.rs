use super::*;

fn shortcuts_state(clipboard_shortcuts: bool) -> ClientShellState {
    let mut config = Config::default();
    config.ui.clipboard_shortcuts = clipboard_shortcuts;
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state
}

fn select_in_pane_1(state: &mut ClientShellState) {
    let mut selection =
        crate::selection::Selection::absolute_range("pane_1".to_owned(), (0, 0), (0, 1));
    assert!(selection.finish());
    state.selection = Some(selection);
}

fn key(code: char) -> crate::input::TerminalKey {
    crate::input::TerminalKey::new(KeyCode::Char(code), KeyModifiers::CONTROL)
}

fn requests_selection_read(outcome: &ClientShellInput) -> bool {
    outcome.actions.iter().any(|action| {
        matches!(
            action,
            ClientShellAction::Endpoint { request, .. }
                if matches!(request.method, crate::api::schema::Method::PaneSelectionRead(_))
        )
    })
}

fn pane_events<'a>(outcome: &'a ClientShellInput, pane: &str) -> Vec<&'a ClientPaneInputEvent> {
    outcome
        .requests
        .iter()
        .filter_map(|request| match request {
            ClientMessage::ClientShellPaneInput { pane_id, events } if pane_id == pane => {
                Some(events.iter())
            }
            _ => None,
        })
        .flatten()
        .collect()
}

fn pasted(outcome: &ClientShellInput, pane: &str) -> Option<String> {
    pane_events(outcome, pane)
        .into_iter()
        .find_map(|event| match event {
            ClientPaneInputEvent::Paste(text) => Some(text.clone()),
            _ => None,
        })
}

fn forwards_a_key(outcome: &ClientShellInput, pane: &str) -> bool {
    pane_events(outcome, pane)
        .into_iter()
        .any(|event| matches!(event, ClientPaneInputEvent::Key { .. }))
}

#[test]
fn a_mouse_selection_stays_highlighted_after_it_is_copied() {
    let mut state = shortcuts_state(true);
    state.compose(106, 20).expect("composed frame");
    let pane = state.hits.panes[0].clone();
    let at = |column_offset: u16, kind| {
        RawInputEvent::Mouse(crossterm::event::MouseEvent {
            kind,
            column: pane.inner_rect.x + column_offset,
            row: pane.inner_rect.y,
            modifiers: KeyModifiers::empty(),
        })
    };
    state.handle_raw_events(vec![at(0, MouseEventKind::Down(MouseButton::Left))]);
    state.handle_raw_events(vec![at(2, MouseEventKind::Drag(MouseButton::Left))]);
    let release = state.handle_raw_events(vec![at(2, MouseEventKind::Up(MouseButton::Left))]);

    assert!(requests_selection_read(&release), "release still copies");
    assert!(state
        .selection
        .as_ref()
        .is_some_and(crate::selection::Selection::is_visible));
}

fn ctrl_alt(code: char) -> crate::input::TerminalKey {
    crate::input::TerminalKey::new(
        KeyCode::Char(code),
        KeyModifiers::CONTROL | KeyModifiers::ALT,
    )
}

#[test]
fn ctrl_alt_c_copies_a_visible_selection() {
    let mut state = shortcuts_state(true);
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(requests_selection_read(&outcome));
    assert!(
        !forwards_a_key(&outcome, "pane_1"),
        "herdr's copy key stays herdr's"
    );
    assert!(state.selection.is_none(), "the copy clears the highlight");
}

#[test]
fn ctrl_alt_c_without_a_selection_reaches_nothing() {
    // A legacy host sends it as ESC 0x03: forwarded, a shell would interrupt.
    let mut state = shortcuts_state(true);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert!(!forwards_a_key(&outcome, "pane_1"));
}

#[test]
fn ctrl_c_reaches_the_pane_even_over_a_visible_selection() {
    let mut state = shortcuts_state(true);
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(key('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert!(forwards_a_key(&outcome, "pane_1"));
}

#[test]
fn ctrl_c_without_a_selection_reaches_the_pane() {
    let mut state = shortcuts_state(true);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(key('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert!(forwards_a_key(&outcome, "pane_1"));
}

#[test]
fn ctrl_alt_p_pastes_clipboard_text_into_the_focused_pane_once_while_held() {
    let mut state = shortcuts_state(true);
    state.read_clipboard_text = || Some("cargo test\n".to_owned());

    let press = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('p'))]);
    assert_eq!(pasted(&press, "pane_1").as_deref(), Some("cargo test\n"));
    assert!(!forwards_a_key(&press, "pane_1"));

    let repeat = state.handle_raw_events(vec![RawInputEvent::Key(
        ctrl_alt('p').with_kind(crossterm::event::KeyEventKind::Repeat),
    )]);
    assert!(repeat.requests.is_empty(), "holding the key pastes once");
}

#[test]
fn ctrl_alt_p_with_no_clipboard_text_reaches_nothing() {
    for reader in [(|| None) as fn() -> Option<String>, || Some(String::new())] {
        let mut state = shortcuts_state(true);
        state.read_clipboard_text = reader;

        let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('p'))]);

        assert!(pasted(&outcome, "pane_1").is_none());
        assert!(!forwards_a_key(&outcome, "pane_1"));
    }
}

#[test]
fn ctrl_v_reaches_the_pane_whatever_the_clipboard_holds() {
    for reader in [
        (|| None) as fn() -> Option<String>,
        || Some(String::new()),
        || Some("text".to_owned()),
    ] {
        let mut state = shortcuts_state(true);
        state.read_clipboard_text = reader;

        let outcome = state.handle_raw_events(vec![RawInputEvent::Key(key('v'))]);

        assert!(pasted(&outcome, "pane_1").is_none());
        assert!(forwards_a_key(&outcome, "pane_1"));
    }
}

#[test]
fn turning_clipboard_shortcuts_off_gives_their_keys_to_the_pane() {
    let mut state = shortcuts_state(false);
    state.read_clipboard_text = || Some("text".to_owned());

    let paste = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('p'))]);
    assert!(pasted(&paste, "pane_1").is_none());
    assert!(forwards_a_key(&paste, "pane_1"));

    select_in_pane_1(&mut state);
    let copy = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);
    assert!(!requests_selection_read(&copy));
    assert!(forwards_a_key(&copy, "pane_1"));
}

fn pane_menu_actions(state: &ClientShellState) -> Vec<ClientContextMenuAction> {
    match state.overlay.as_ref() {
        Some(ClientShellOverlay::ContextMenu(menu)) => {
            menu.items().iter().map(|item| item.action).collect()
        }
        _ => Vec::new(),
    }
}

/// The menu index of Paste, where this platform offers it.
fn paste_item(state: &ClientShellState) -> Option<usize> {
    pane_menu_actions(state)
        .iter()
        .position(|action| *action == ClientContextMenuAction::Paste)
}

#[test]
fn the_pane_menu_offers_copy_over_a_selection_and_paste_where_it_can_read() {
    let mut state = shortcuts_state(true);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);
    assert!(!pane_menu_actions(&state).contains(&ClientContextMenuAction::Copy));
    assert_eq!(
        paste_item(&state),
        crate::platform::CAN_READ_CLIPBOARD_TEXT.then_some(0),
        "Paste leads the menu where clipboard text can be read, and is absent elsewhere"
    );

    state.overlay = None;
    select_in_pane_1(&mut state);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);
    assert_eq!(
        pane_menu_actions(&state).first(),
        Some(&ClientContextMenuAction::Copy)
    );
    assert_eq!(
        paste_item(&state),
        crate::platform::CAN_READ_CLIPBOARD_TEXT.then_some(1)
    );
}

#[test]
fn the_menus_copy_copies_and_clears_the_selection() {
    let mut state = shortcuts_state(true);
    select_in_pane_1(&mut state);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);

    let mut outcome = ClientShellInput::default();
    state.activate_context_menu_item(0, &mut outcome);

    assert!(requests_selection_read(&outcome));
    assert!(state.selection.is_none());
}

#[test]
fn the_menus_paste_goes_to_the_right_clicked_pane_and_focuses_it() {
    let mut state = shortcuts_state(true);
    state.read_clipboard_text = || Some("ls".to_owned());
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);
    let Some(paste) = paste_item(&state) else {
        return;
    };

    let mut outcome = ClientShellInput::default();
    state.activate_context_menu_item(paste, &mut outcome);

    assert_eq!(pasted(&outcome, "pane_1").as_deref(), Some("ls"));
    assert!(outcome.actions.iter().any(|action| matches!(
        action,
        ClientShellAction::Endpoint { request, .. }
            if matches!(&request.method,
                crate::api::schema::Method::PaneFocus(target) if target.pane_id == "pane_1")
    )));
}

#[test]
fn the_menus_paste_says_so_when_there_is_nothing_to_paste() {
    let mut state = shortcuts_state(true);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);
    let Some(paste) = paste_item(&state) else {
        return;
    };

    let mut outcome = ClientShellInput::default();
    state.activate_context_menu_item(paste, &mut outcome);

    assert!(outcome.requests.is_empty());
    assert_eq!(
        state
            .copy_feedback
            .as_ref()
            .map(|feedback| feedback.message.as_str()),
        Some("nothing to paste: no text on the clipboard, or it could not be read")
    );
}

#[test]
fn a_configured_ctrl_alt_p_binding_wins_over_the_paste_shortcut() {
    let mut config = Config::default();
    config.keys.zoom = crate::config::BindingConfig::One("ctrl+alt+p".to_owned());
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state.read_clipboard_text = || Some("must not paste".to_owned());

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('p'))]);

    assert!(pasted(&outcome, "pane_1").is_none());
    assert!(
        !forwards_a_key(&outcome, "pane_1"),
        "the binding consumed the key"
    );
}

#[test]
fn turning_clipboard_shortcuts_off_restores_upstreams_pane_menu() {
    let mut state = shortcuts_state(false);
    select_in_pane_1(&mut state);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);

    let actions = pane_menu_actions(&state);
    assert!(!actions.contains(&ClientContextMenuAction::Copy));
    assert!(!actions.contains(&ClientContextMenuAction::Paste));
    assert_eq!(actions.first(), Some(&ClientContextMenuAction::RenamePane));
}

#[test]
fn the_menus_paste_clears_the_selection_as_a_host_paste_does() {
    let mut state = shortcuts_state(true);
    state.read_clipboard_text = || Some("ls".to_owned());
    select_in_pane_1(&mut state);
    state.open_pane_context_menu("pane_1".to_owned(), 10, 5);
    let Some(paste) = paste_item(&state) else {
        return;
    };

    let mut outcome = ClientShellInput::default();
    state.activate_context_menu_item(paste, &mut outcome);

    assert_eq!(pasted(&outcome, "pane_1").as_deref(), Some("ls"));
    assert!(state.selection.is_none());
}

#[test]
fn without_copy_on_select_ctrl_alt_c_copies_a_retained_selection_even_with_shortcuts_off() {
    let mut config = Config::default();
    config.ui.clipboard_shortcuts = false;
    config.ui.copy_on_select = false;
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(
        requests_selection_read(&outcome),
        "the selection has no other key"
    );
    assert!(!forwards_a_key(&outcome, "pane_1"));
}

#[test]
fn a_configured_ctrl_alt_c_binding_wins_over_the_copy_key() {
    let mut config = Config::default();
    config.keys.zoom = crate::config::BindingConfig::One("ctrl+alt+c".to_owned());
    assert!(config.collect_diagnostics().is_empty());
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(
        !requests_selection_read(&outcome),
        "the binding took the key"
    );
    assert!(!outcome.actions.is_empty(), "the binding ran");
}

#[test]
fn ctrl_alt_c_after_the_prefix_is_the_prefix_commands_key() {
    let mut state = shortcuts_state(true);
    state.handle_raw_events(vec![RawInputEvent::Key(crate::input::TerminalKey::new(
        KeyCode::Char('6'),
        KeyModifiers::CONTROL,
    ))]);
    assert_eq!(state.mode, ClientShellMode::Prefix);
    // Pressing the prefix clears a selection; make one visible after it.
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert_ne!(state.mode, ClientShellMode::Prefix, "prefix mode ended");
}

#[test]
fn a_prefix_on_ctrl_alt_c_enters_prefix_mode() {
    let config: Config = toml::from_str("[keys]\nprefix = \"ctrl+alt+c\"\n").expect("parses");
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    select_in_pane_1(&mut state);

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert_eq!(state.mode, ClientShellMode::Prefix);
}

#[test]
fn ctrl_alt_c_copies_a_selection_dragged_in_navigate_mode() {
    let mut state = shortcuts_state(true);
    state.compose(106, 20).expect("composed frame");
    state.mode = ClientShellMode::Navigate;
    let pane = state.hits.panes[0].clone();
    let at = |column_offset: u16, kind| {
        RawInputEvent::Mouse(crossterm::event::MouseEvent {
            kind,
            column: pane.inner_rect.x + column_offset,
            row: pane.inner_rect.y,
            modifiers: KeyModifiers::empty(),
        })
    };
    state.handle_raw_events(vec![at(0, MouseEventKind::Down(MouseButton::Left))]);
    state.handle_raw_events(vec![at(2, MouseEventKind::Drag(MouseButton::Left))]);
    state.handle_raw_events(vec![at(2, MouseEventKind::Up(MouseButton::Left))]);
    assert_eq!(state.mode, ClientShellMode::Navigate);
    assert!(state
        .selection
        .as_ref()
        .is_some_and(crate::selection::Selection::is_visible));

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(
        requests_selection_read(&outcome),
        "the selection was copied, not dropped"
    );
}

#[test]
fn in_navigate_mode_with_nothing_selected_a_navigate_binding_keeps_ctrl_alt_c() {
    let config: Config =
        toml::from_str("[keys]\nnavigate_workspace_up = \"ctrl+alt+c\"\n").expect("parses");
    assert!(config.collect_diagnostics().is_empty());
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&config));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state.mode = ClientShellMode::Navigate;

    let outcome = state.handle_raw_events(vec![RawInputEvent::Key(ctrl_alt('c'))]);

    assert!(!requests_selection_read(&outcome));
    assert!(
        outcome.repaint || !outcome.actions.is_empty() || !outcome.requests.is_empty(),
        "the navigate binding handled the key"
    );
}

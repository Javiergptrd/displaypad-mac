"""Ready-made pages for popular apps: the shortcuts people use most, one per key, with the
page linked to the app so the pad shows it while the app is in front.

Every shortcut is the app's own macOS default (a user who changed theirs can edit the key).
They use letters, numbers and named keys only, not punctuation like [ ] / \\ =: the pad
sends key positions, and on a keyboard that isn't US English those positions type other
characters, so a punctuation shortcut would do something else there.

An app with no default shortcuts is driven by another action instead: OBS through its
WebSocket server (obs.py). Its page stays up while OBS is open ("when": "open"), since
you control OBS from the game or the app you are streaming, and its "setup" says what the
user turns on first.

Keys 1 to 10 (11 when there is room for one more) hold the shortcuts and key 12 goes back
to the page shown before. App names are matched as appwatch.norm() does, so "Adobe
Photoshop" also finds "Adobe Photoshop 2026".
"""

import json
from typing import Dict, Iterable, List, Optional

from . import appwatch


def _obs(do: str, **kw) -> dict:
    return {"type": "obs", "do": do, **kw}


# Chrome, Edge and Brave share Chromium's default shortcuts.
_CHROMIUM = [
    ("cmd+t", "➕", "New tab", "Nueva pestaña"),
    ("cmd+shift+t", "↩️", "Reopen tab", "Reabrir última"),
    ("cmd+w", "✖️", "Close tab", "Cerrar pestaña"),
    ("cmd+l", "🔗", "Address bar", "Barra de URL"),
    ("cmd+alt+right", "▶️", "Next tab", "Sig. pestaña"),
    ("cmd+alt+left", "◀️", "Prev tab", "Ant. pestaña"),
    ("cmd+left", "⬅️", "Back", "Atrás"),
    ("cmd+r", "🔄", "Reload", "Recargar"),
    ("cmd+shift+n", "🕶️", "Incognito", "Incógnito"),
    ("cmd+f", "🔍", "Find", "Buscar"),
]

# (shortcut or action, emoji, English label, Spanish label[, True: held while the pad key is held])
PRESETS: List[dict] = [
    {"id": "obs", "name": "OBS Studio", "apps": ["OBS", "OBS Studio"], "when": "open", "setup": "obs",
     "color": "#25232B", "text": "#FFFFFF", "keys": [
        (_obs("stream"), "🔴", "Go live", "En vivo"),
        (_obs("record"), "⏺️", "Record", "Grabar"),
        (_obs("pause"), "⏸️", "Pause rec", "Pausar"),
        (_obs("mute"), "🎙️", "Mic", "Micrófono"),
        (_obs("virtualcam"), "📷", "Virtual cam", "Cám. virtual"),
        (_obs("next_scene"), "⏭️", "Next scene", "Escena sig."),
        (_obs("scene", scene="#1"), "1️⃣", "Scene 1", "Escena 1"),
        (_obs("scene", scene="#2"), "2️⃣", "Scene 2", "Escena 2"),
        (_obs("scene", scene="#3"), "3️⃣", "Scene 3", "Escena 3"),
        (_obs("scene", scene="#4"), "4️⃣", "Scene 4", "Escena 4"),
        (_obs("scene", scene="#5"), "5️⃣", "Scene 5", "Escena 5"),
    ]},
    {"id": "photoshop", "name": "Photoshop", "apps": ["Adobe Photoshop"], "color": "#001E36", "text": "#31A8FF", "keys": [
        ("cmd+z", "↩️", "Undo", "Deshacer"),
        ("cmd+shift+z", "↪️", "Redo", "Rehacer"),
        ("cmd+t", "📐", "Transform", "Transformar"),
        ("cmd+alt+shift+n", "🆕", "New layer", "Nueva capa"),
        ("cmd+j", "📑", "Duplicate", "Duplicar"),
        ("cmd+e", "🔽", "Merge", "Combinar"),
        ("cmd+d", "🚫", "Deselect", "Deseleccionar"),
        ("cmd+shift+i", "🔄", "Invert", "Invertir"),
        ("cmd+0", "🔍", "Fit screen", "Encajar"),
        ("cmd+alt+shift+w", "🚀", "Export as", "Exportar como"),
    ]},
    {"id": "illustrator", "name": "Illustrator", "apps": ["Adobe Illustrator"], "color": "#330000", "text": "#FF9A00", "keys": [
        ("cmd+z", "↩️", "Undo", "Deshacer"),
        ("cmd+shift+z", "↪️", "Redo", "Rehacer"),
        ("cmd+g", "🗂️", "Group", "Agrupar"),
        ("cmd+shift+g", "📂", "Ungroup", "Desagrupar"),
        ("cmd+2", "🔒", "Lock", "Bloquear"),
        ("cmd+alt+2", "🔓", "Unlock all", "Desbloquear"),
        ("cmd+7", "🎭", "Clip mask", "Máscara"),
        ("cmd+shift+o", "✒️", "Outlines", "Contornos"),
        ("cmd+y", "👁️", "Outline view", "Vista contorno"),
        ("cmd+0", "🔍", "Fit artboard", "Encajar"),
    ]},
    {"id": "premiere", "name": "Premiere Pro", "apps": ["Adobe Premiere Pro"], "color": "#00005B", "text": "#9999FF", "keys": [
        ("space", "⏯️", "Play/Stop", "Reproducir"),
        ("cmd+k", "✂️", "Add edit", "Cortar"),
        ("shift+forwarddelete", "🗑️", "Ripple delete", "Borrar y cerrar"),
        ("i", "📥", "Mark in", "Entrada"),
        ("o", "📤", "Mark out", "Salida"),
        ("m", "🔖", "Marker", "Marcador"),
        ("up", "⏮️", "Prev edit", "Corte ant."),
        ("down", "⏭️", "Next edit", "Corte sig."),
        ("cmd+z", "↩️", "Undo", "Deshacer"),
        ("cmd+m", "🚀", "Export", "Exportar"),
    ]},
    {"id": "aftereffects", "name": "After Effects", "apps": ["Adobe After Effects"], "color": "#1A0033", "text": "#D291FF", "keys": [
        ("space", "▶️", "Preview", "Previsualizar"),
        ("cmd+shift+d", "✂️", "Split layer", "Dividir capa"),
        ("f9", "📈", "Easy ease", "Suavizado"),
        ("u", "🔑", "Keyframes", "Claves"),
        ("cmd+shift+c", "📦", "Pre-compose", "Precomponer"),
        ("cmd+y", "🟥", "New solid", "Sólido nuevo"),
        ("cmd+n", "🎞️", "New comp", "Nueva comp."),
        ("cmd+d", "📑", "Duplicate", "Duplicar"),
        ("cmd+z", "↩️", "Undo", "Deshacer"),
        ("cmd+alt+m", "🚀", "Media Encoder", "Media Encoder"),
    ]},
    {"id": "lightroom", "name": "Lightroom Classic", "apps": ["Adobe Lightroom Classic"], "color": "#001E36", "text": "#ADD5EC", "keys": [
        ("p", "🏳️", "Pick", "Marcar"),
        ("x", "❌", "Reject", "Rechazar"),
        ("5", "⭐", "5 stars", "5 estrellas"),
        ("g", "🖼️", "Grid", "Cuadrícula"),
        ("d", "🎛️", "Develop", "Revelar"),
        ("r", "✂️", "Crop", "Recortar"),
        ("y", "🌓", "Before/After", "Antes/Después"),
        ("cmd+shift+c", "📋", "Copy settings", "Copiar ajustes"),
        ("cmd+shift+v", "📌", "Paste settings", "Pegar ajustes"),
        ("cmd+shift+e", "🚀", "Export", "Exportar"),
    ]},
    {"id": "resolve", "name": "DaVinci Resolve", "apps": ["DaVinci Resolve", "DaVinci Resolve Studio"], "color": "#1A1A1A", "text": "#F0A830", "keys": [
        ("space", "⏯️", "Play/Stop", "Reproducir"),
        ("cmd+b", "✂️", "Split", "Dividir"),
        ("shift+delete", "🗑️", "Ripple delete", "Borrar y cerrar"),
        ("i", "📥", "Mark in", "Entrada"),
        ("o", "📤", "Mark out", "Salida"),
        ("m", "🔖", "Marker", "Marcador"),
        ("b", "🔪", "Blade", "Cuchilla"),
        ("a", "🖱️", "Select", "Selección"),
        ("n", "🧲", "Snapping", "Imán"),
        ("shift+z", "🔍", "Zoom to fit", "Encajar"),
    ]},
    {"id": "finalcut", "name": "Final Cut Pro", "apps": ["Final Cut Pro", "Final Cut Pro Trial"], "color": "#1B1B2F", "text": "#C9B6FF", "keys": [
        ("space", "⏯️", "Play/Pause", "Reproducir"),
        ("cmd+b", "✂️", "Blade", "Cortar"),
        ("delete", "🗑️", "Delete", "Eliminar"),
        ("i", "📥", "Mark in", "Entrada"),
        ("o", "📤", "Mark out", "Salida"),
        ("m", "🔖", "Marker", "Marcador"),
        ("q", "🔗", "Connect", "Conectar"),
        ("e", "⤵️", "Append", "Añadir al final"),
        ("shift+z", "🔍", "Zoom to fit", "Encajar"),
        ("cmd+e", "🚀", "Share", "Compartir"),
    ]},
    {"id": "logic", "name": "Logic Pro", "apps": ["Logic Pro", "Logic Pro X"], "color": "#1C1C1E", "text": "#7FE0B0", "keys": [
        ("space", "⏯️", "Play/Stop", "Reproducir"),
        ("r", "⏺️", "Record", "Grabar"),
        ("return", "⏮️", "To start", "Al inicio"),
        ("c", "🔁", "Cycle", "Ciclo"),
        ("k", "⏱️", "Metronome", "Metrónomo"),
        ("cmd+t", "✂️", "Split", "Dividir"),
        ("x", "🎚️", "Mixer", "Mezclador"),
        ("e", "🎹", "Editors", "Editores"),
        ("cmd+alt+n", "🆕", "New track", "Nueva pista"),
        ("cmd+b", "💿", "Bounce", "Bounce"),
    ]},
    {"id": "figma", "name": "Figma", "apps": ["Figma"], "color": "#1E1E1E", "text": "#C7A6FF", "keys": [
        ("f", "🔳", "Frame", "Marco"),
        ("r", "⬜", "Rectangle", "Rectángulo"),
        ("t", "🔤", "Text", "Texto"),
        ("p", "✒️", "Pen", "Pluma"),
        ("cmd+g", "🗂️", "Group", "Agrupar"),
        ("shift+a", "📏", "Auto layout", "Auto layout"),
        ("cmd+alt+k", "🧩", "Component", "Componente"),
        ("cmd+d", "📑", "Duplicate", "Duplicar"),
        ("shift+1", "🔍", "Zoom to fit", "Encajar"),
        ("cmd+shift+e", "🚀", "Export", "Exportar"),
    ]},
    {"id": "vscode", "name": "VS Code", "apps": ["Visual Studio Code"], "color": "#1E1E1E", "text": "#3FA9F5", "keys": [
        ("cmd+shift+p", "🎛️", "Commands", "Comandos"),
        ("cmd+p", "📂", "Quick open", "Abrir rápido"),
        ("cmd+b", "🗄️", "Sidebar", "Barra lateral"),
        ("cmd+j", "💻", "Panel", "Panel"),
        ("cmd+shift+f", "🔎", "Search", "Buscar"),
        ("ctrl+shift+g", "🌿", "Git", "Git"),
        ("shift+alt+f", "✨", "Format", "Formatear"),
        ("f12", "🎯", "Definition", "Definición"),
        ("f2", "✏️", "Rename", "Renombrar"),
        ("f5", "🐞", "Debug", "Depurar"),
    ]},
    {"id": "zoom", "name": "Zoom", "apps": ["zoom.us"], "color": "#0B2A66", "text": "#FFFFFF", "keys": [
        ("cmd+shift+a", "🎙️", "Mute", "Silenciar"),
        ("cmd+shift+v", "📹", "Video", "Video"),
        ("cmd+shift+s", "🖥️", "Share", "Compartir"),
        ("alt+y", "✋", "Raise hand", "Alzar mano"),
        ("cmd+shift+h", "💬", "Chat", "Chat"),
        ("cmd+u", "👥", "Participants", "Participantes"),
        ("cmd+shift+w", "🔲", "View", "Vista"),
        ("cmd+shift+r", "⏺️", "Record", "Grabar"),
        ("cmd+shift+f", "🖼️", "Full screen", "Maximizar"),
        ("cmd+w", "🚪", "Leave", "Salir"),
        ("space", "🗣️", "Push to talk", "Hablar", True),
    ]},
    {"id": "teams", "name": "Microsoft Teams", "apps": ["Microsoft Teams", "Microsoft Teams (work or school)"], "color": "#2E2F6B", "text": "#FFFFFF", "keys": [
        ("cmd+shift+m", "🎙️", "Mute", "Silenciar"),
        ("cmd+shift+o", "📹", "Camera", "Cámara"),
        ("cmd+shift+e", "🖥️", "Share", "Compartir"),
        ("cmd+shift+k", "✋", "Raise hand", "Alzar mano"),
        ("cmd+shift+p", "🌫️", "Blur", "Desenfocar"),
        ("cmd+shift+a", "🎥", "Accept video", "Aceptar video"),
        ("cmd+shift+s", "📞", "Accept audio", "Aceptar audio"),
        ("cmd+shift+d", "❌", "Decline", "Rechazar"),
        ("cmd+shift+h", "🚪", "Hang up", "Colgar"),
        ("alt+space", "🗣️", "Push to talk", "Hablar", True),
    ]},
    {"id": "chrome", "name": "Google Chrome", "apps": ["Google Chrome", "Google Chrome Beta", "Google Chrome Dev",
     "Google Chrome Canary", "Google Chrome for Testing", "Chromium"], "color": "#202124", "text": "#8AB4F8",
     "keys": _CHROMIUM},
    {"id": "edge", "name": "Microsoft Edge", "apps": ["Microsoft Edge", "Microsoft Edge Beta", "Microsoft Edge Dev",
     "Microsoft Edge Canary"], "color": "#0C2A3E", "text": "#5ED3F3", "keys": _CHROMIUM},
    {"id": "brave", "name": "Brave", "apps": ["Brave Browser", "Brave Browser Beta", "Brave Browser Nightly"],
     "color": "#1F1F2A", "text": "#FF7A4D", "keys": _CHROMIUM},
    {"id": "safari", "name": "Safari", "apps": ["Safari", "Safari Technology Preview"], "color": "#0A2540", "text": "#5AC8FA", "keys": [
        ("cmd+t", "➕", "New tab", "Nueva pestaña"),
        ("cmd+shift+t", "↩️", "Reopen tab", "Reabrir última"),
        ("cmd+w", "✖️", "Close tab", "Cerrar pestaña"),
        ("cmd+l", "🔗", "Address bar", "Barra de URL"),
        ("ctrl+tab", "▶️", "Next tab", "Sig. pestaña"),
        ("ctrl+shift+tab", "◀️", "Prev tab", "Ant. pestaña"),
        ("cmd+r", "🔄", "Reload", "Recargar"),
        ("cmd+shift+r", "📖", "Reader", "Lector"),
        ("cmd+shift+n", "🕶️", "Private", "Privada"),
        ("cmd+f", "🔍", "Find", "Buscar"),
    ]},
    {"id": "spotify", "name": "Spotify", "apps": ["Spotify"], "color": "#121212", "text": "#1ED760", "keys": [
        ("space", "⏯️", "Play/Pause", "Play/Pausa"),
        ("cmd+right", "⏭️", "Next", "Siguiente"),
        ("cmd+left", "⏮️", "Previous", "Anterior"),
        ("cmd+up", "🔊", "Volume up", "Subir volumen"),
        ("cmd+down", "🔉", "Volume down", "Bajar volumen"),
        ("alt+shift+b", "❤️", "Like", "Me gusta"),
        ("cmd+s", "🔀", "Shuffle", "Aleatorio"),
        ("cmd+r", "🔁", "Repeat", "Repetir"),
        ("cmd+k", "🔍", "Search", "Buscar"),
        ("alt+shift+q", "📋", "Queue", "Cola"),
    ]},
    {"id": "slack", "name": "Slack", "apps": ["Slack"], "color": "#4A154B", "text": "#ECB22E", "keys": [
        ("cmd+k", "🧭", "Jump to", "Ir a chat"),
        ("cmd+g", "🔍", "Search", "Buscar"),
        ("alt+shift+down", "📬", "Next unread", "Sig. no leído"),
        ("cmd+shift+a", "📥", "All unreads", "No leídos"),
        ("cmd+shift+t", "🧵", "Threads", "Hilos"),
        ("cmd+shift+m", "🔔", "Activity", "Actividad"),
        ("cmd+shift+h", "🎧", "Huddle", "Huddle"),
        ("cmd+shift+space", "🎙️", "Huddle mute", "Silenciar"),
        ("cmd+n", "✏️", "New message", "Nuevo mensaje"),
        ("cmd+shift+y", "😀", "Set status", "Estado"),
    ]},
    {"id": "discord", "name": "Discord", "apps": ["Discord", "Discord PTB", "Discord Canary"], "color": "#1E1F22", "text": "#A5ACFA", "keys": [
        ("cmd+shift+m", "🎙️", "Mute mic", "Silenciar mic"),
        ("cmd+shift+d", "🎧", "Deafen", "Ensordecer"),
        ("cmd+k", "🧭", "Quick switch", "Ir a canal"),
        ("alt+up", "⬆️", "Prev channel", "Canal anterior"),
        ("alt+down", "⬇️", "Next channel", "Sig. canal"),
        ("esc", "✅", "Mark read", "Marcar leído"),
        ("cmd+f", "🔍", "Search", "Buscar"),
        ("cmd+e", "😀", "Emoji", "Emoji"),
        ("cmd+p", "📌", "Pins", "Fijados"),
        ("cmd+shift+u", "📎", "Upload file", "Subir archivo"),
    ]},
    {"id": "blender", "name": "Blender", "apps": ["Blender"], "color": "#232323", "text": "#F5792A", "keys": [
        ("tab", "✏️", "Edit mode", "Modo edición"),
        ("shift+a", "➕", "Add", "Añadir"),
        ("shift+d", "📑", "Duplicate", "Duplicar"),
        ("g", "✋", "Move", "Mover"),
        ("r", "🔄", "Rotate", "Rotar"),
        ("s", "📏", "Scale", "Escalar"),
        ("kpdecimal", "🎯", "Frame sel.", "Encuadrar sel."),
        ("kp0", "🎥", "Camera view", "Vista cámara"),
        ("kp7", "🔝", "Top view", "Vista superior"),
        ("f12", "📸", "Render", "Renderizar"),
    ]},
    {"id": "excel", "name": "Excel", "apps": ["Microsoft Excel"], "color": "#0E3B22", "text": "#7FD8A6", "keys": [
        ("cmd+ctrl+v", "📋", "Paste special", "Pegado espec."),
        ("cmd+shift+f", "🔽", "Filter", "Filtro"),
        ("cmd+shift+r", "↕️", "Sort", "Ordenar"),
        ("cmd+shift+t", "🧮", "AutoSum", "Autosuma"),
        ("cmd+1", "🎨", "Format cells", "Formato celdas"),
        ("cmd+d", "⬇️", "Fill down", "Rellenar abajo"),
        ("cmd+t", "📊", "Make table", "Crear tabla"),
        ("f2", "✏️", "Edit cell", "Editar celda"),
        ("alt+right", "▶️", "Next sheet", "Hoja siguiente"),
        ("alt+left", "◀️", "Prev sheet", "Hoja anterior"),
    ]},
    {"id": "word", "name": "Word", "apps": ["Microsoft Word"], "color": "#0F2B5B", "text": "#9CC3FF", "keys": [
        ("cmd+alt+1", "1️⃣", "Heading 1", "Título 1"),
        ("cmd+alt+2", "2️⃣", "Heading 2", "Título 2"),
        ("cmd+shift+n", "📝", "Normal style", "Estilo normal"),
        ("cmd+shift+l", "📃", "Bullet list", "Viñetas"),
        ("cmd+shift+v", "📋", "Paste plain", "Pegar texto"),
        ("cmd+shift+e", "🕵️", "Track edits", "Ctrl. cambios"),
        ("cmd+alt+a", "💬", "Comment", "Comentario"),
        ("ctrl+h", "🔁", "Replace", "Reemplazar"),
        ("cmd+return", "📄", "Page break", "Salto página"),
        ("cmd+alt+l", "✅", "Spelling", "Ortografía"),
    ]},
    {"id": "powerpoint", "name": "PowerPoint", "apps": ["Microsoft PowerPoint"], "color": "#4A1A0C", "text": "#FFA88A", "keys": [
        ("cmd+shift+return", "▶️", "Start show", "Presentar"),
        ("alt+return", "🖥️", "Presenter", "Moderador"),
        ("cmd+shift+n", "➕", "New slide", "Nueva diapo."),
        ("cmd+shift+d", "📑", "Dup. slide", "Duplicar diap."),
        ("cmd+alt+g", "🔗", "Group", "Agrupar"),
        ("cmd+alt+shift+g", "✂️", "Ungroup", "Desagrupar"),
        ("cmd+shift+c", "🖌️", "Copy format", "Copiar formato"),
        ("cmd+shift+v", "🎨", "Paste format", "Pegar formato"),
        ("cmd+shift+f", "⬆️", "To front", "Al frente"),
        ("cmd+shift+b", "⬇️", "To back", "Al fondo"),
    ]},
    {"id": "outlook", "name": "Outlook", "apps": ["Microsoft Outlook"], "color": "#0A2A52", "text": "#5DB8F5", "keys": [
        ("cmd+n", "✉️", "New email", "Nuevo correo"),
        ("cmd+r", "↩️", "Reply", "Responder"),
        ("cmd+shift+r", "👥", "Reply all", "Resp. a todos"),
        ("cmd+j", "➡️", "Forward", "Reenviar"),
        ("cmd+return", "📤", "Send", "Enviar"),
        ("cmd+t", "✅", "Mark read", "Marcar leído"),
        ("cmd+shift+m", "📁", "Move", "Mover"),
        ("cmd+alt+f", "🔍", "Search", "Buscar"),
        ("cmd+2", "📅", "Calendar", "Calendario"),
    ]},
    {"id": "xcode", "name": "Xcode", "apps": ["Xcode", "Xcode-beta"], "color": "#1C1C1E", "text": "#4FA8FF", "keys": [
        ("cmd+r", "▶️", "Run", "Ejecutar"),
        ("cmd+b", "🔨", "Build", "Compilar"),
        ("cmd+u", "🧪", "Test", "Probar"),
        ("cmd+shift+k", "🧹", "Clean build", "Limpiar"),
        ("cmd+shift+o", "⚡", "Open quickly", "Abrir rápido"),
        ("cmd+shift+f", "🔍", "Find in proj.", "Buscar todo"),
        ("cmd+ctrl+j", "🎯", "Definition", "Definición"),
        ("cmd+ctrl+left", "⬅️", "Go back", "Atrás"),
        ("cmd+shift+y", "🐞", "Debug area", "Área depurac."),
        ("cmd+alt+return", "🖼️", "Preview", "Vista previa"),
    ]},
    {"id": "obsidian", "name": "Obsidian", "apps": ["Obsidian"], "color": "#1E1A2E", "text": "#A88BFA", "keys": [
        ("cmd+o", "🧭", "Quick switch", "Abrir nota"),
        ("cmd+p", "🎛️", "Commands", "Comandos"),
        ("cmd+n", "📝", "New note", "Nueva nota"),
        ("cmd+shift+f", "🔍", "Search vault", "Buscar bóveda"),
        ("cmd+e", "👁️", "Read/Edit", "Leer/Editar"),
        ("cmd+g", "🕸️", "Graph view", "Vista de grafo"),
        ("cmd+alt+left", "⬅️", "Back", "Atrás"),
        ("cmd+alt+right", "➡️", "Forward", "Adelante"),
        ("cmd+t", "➕", "New tab", "Nueva pestaña"),
        ("cmd+k", "🔗", "Insert link", "Añadir enlace"),
    ]},
    {"id": "audacity", "name": "Audacity", "apps": ["Audacity", "Audacity 4"], "color": "#0D1B3E", "text": "#FFB547", "keys": [
        ("space", "⏯️", "Play/Stop", "Play/Detener"),
        ("r", "⏺️", "Record", "Grabar"),
        ("p", "⏸️", "Pause", "Pausa"),
        ("home", "⏮️", "To start", "Ir al inicio"),
        ("cmd+i", "✂️", "Split", "Dividir"),
        ("cmd+d", "📑", "Duplicate", "Duplicar"),
        ("cmd+t", "🎯", "Trim", "Recortar"),
        ("cmd+b", "🏷️", "Add label", "Etiqueta"),
        ("cmd+f", "↔️", "Fit project", "Ajustar todo"),
        ("cmd+shift+e", "💾", "Export", "Exportar"),
    ]},
]

BACK_KEY = "12"
_BACK = {"en": "Back", "es": "Atrás"}
_BY_ID = {p["id"]: p for p in PRESETS}


def get(preset_id: str) -> Optional[dict]:
    return _BY_ID.get(str(preset_id).strip().lower())


def for_app(name: str) -> Optional[dict]:
    """The preset for an app name ("Adobe Photoshop 2026"), or None."""
    n = appwatch.norm(name)
    return next((p for p in PRESETS if n in map(appwatch.norm, p["apps"])), None)


def keys(preset: dict, lang: str = "en") -> Dict[str, dict]:
    """The page's keys, numbered like the config ("1".."12"), labels in lang (en or es)."""
    es = lang == "es"
    out = {}
    for i, (combo, emoji, en, es_label, *hold) in enumerate(preset["keys"], 1):
        action = dict(combo) if isinstance(combo, dict) else {"type": "shortcut", "keys": combo}
        if hold and hold[0]:
            action["hold"] = True
        out[str(i)] = {"emoji": emoji, "label": es_label if es else en, "color": preset["color"],
                       "text_color": preset["text"], "actions": [action]}
    out[BACK_KEY] = {"emoji": "⬅️", "label": _BACK["es" if es else "en"], "color": "#2A2A32",
                     "actions": [{"type": "page", "page": "back"}]}
    return out


def page(preset: dict, lang: str = "en", installed: Iterable[str] = ()) -> dict:
    """A whole page: its keys, linked to the app while it is in front (or open, for OBS).
    The link uses the names of the matching installed apps, or the preset's own names when
    none is installed (they still match a later install, whatever its year)."""
    return {"apps": installed_names(preset, installed) or list(preset["apps"]), "when": preset.get("when", "front"),
            "keys": keys(preset, lang)}


def action_id(action: dict) -> str:
    """The same shortcut or action, however it is written: to tell which ones a page has."""
    if action.get("type") == "shortcut":
        return "shortcut:" + str(action.get("keys", "")).replace(" ", "").lower()
    return json.dumps(action, sort_keys=True, ensure_ascii=False)


def installed_names(preset: dict, installed: Iterable[str]) -> List[str]:
    """The installed apps this preset is for, under their own names."""
    want = set(map(appwatch.norm, preset["apps"]))
    return [n for n in installed if appwatch.norm(n) in want]


def catalog(lang: str = "en", installed: Iterable[str] = ()) -> List[dict]:
    """For the configurator and the MCP tools: every preset, what it puts on the keys, which
    of its apps are installed, and the page it makes (as page() does). Installed ones first."""
    installed = list(installed)
    out = []
    for p in PRESETS:
        ks = keys(p, lang)
        out.append({"id": p["id"], "name": p["name"], "apps": list(p["apps"]),
                    "installed": installed_names(p, installed), "page": page(p, lang, installed),
                    **({"setup": p["setup"]} if p.get("setup") else {}),
                    "keys": [{"key": n, "emoji": k["emoji"], "label": k["label"],
                              "shortcut": k["actions"][0].get("keys", ""), "action": k["actions"][0],
                              **({"hold": True} if k["actions"][0].get("hold") else {})}
                             for n, k in ks.items() if k["actions"][0]["type"] != "page"]})
    out.sort(key=lambda p: not p["installed"])
    return out

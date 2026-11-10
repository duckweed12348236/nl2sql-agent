/** 界面图标：Element Plus 风格的内联 SVG（24×24 描边），不使用 emoji */
export const ICON_PATHS = {
  database:
    '<ellipse cx="12" cy="5.5" rx="7" ry="3"/><path d="M5 8.5v10c0 1.7 3.1 3 7 3s7-1.3 7-3v-10"/><path d="M5 13.5c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M9.5 9.5v10"/>',
  metric: '<path d="M6 20v-8M12 20V5M18 20v-6M3.5 20h17"/>',
  relation:
    '<path d="M9.6 14.4l4.8-4.8"/><path d="M12.9 6.3l1.7-1.7a4.1 4.1 0 0 1 5.8 5.8l-1.7 1.7"/><path d="M11.1 17.7l-1.7 1.7a4.1 4.1 0 0 1-5.8-5.8l1.7-1.7"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  plus: '<path d="M12 5.5v13M5.5 12h13"/>',
  edit: '<path d="M4.5 19.5l4.2-.9L19.2 8.1a2.1 2.1 0 0 0-3-3L5.4 15.3z"/><path d="M14.7 6.6l2.7 2.7"/>',
  trash:
    '<path d="M4.5 7h15"/><path d="M9.5 7V4.8h5V7"/><path d="M6.6 7l.9 12.2a1 1 0 0 0 1 .9h7a1 1 0 0 0 1-.9L17.4 7"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  'arrow-right': '<path d="M4.5 12h15M13.5 6l6 6-6 6"/>',
  copy: '<rect x="9" y="9" width="10.5" height="10.5" rx="2"/><path d="M15.5 6.8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h.8"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  dataset:
    '<path d="M6.5 3.5h7l4.5 4.5v12a1 1 0 0 1-1 1h-10.5a1 1 0 0 1-1-1v-15.5a1 1 0 0 1 1-1z"/><path d="M13.5 3.5V8H18"/><path d="M8.5 12.5h7M8.5 16h4.5"/>',
  play: '<path d="M8 5.2l10.5 6.8L8 18.8z"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.4V12l3.2 1.9"/>',
  ask: '<path d="M20.5 12.4c0 3.9-3.8 7-8.5 7-.9 0-1.8-.1-2.6-.3L4.5 20.5l1.6-3.5C4.7 15.9 3.5 14.3 3.5 12.4c0-3.9 3.8-7 8.5-7s8.5 3.1 8.5 7z"/><path d="M9.7 11.1a2.3 2.3 0 1 1 3 2.2c-.7.3-1.2.7-1.2 1.5"/><path d="M11.5 17.3h.01"/>',
  collect: '<path d="M12 4.5v9.5"/><path d="M8 10.6l4 4 4-4"/><path d="M4.5 17.5v2h15v-2"/>',
  model:
    '<rect x="7.5" y="7.5" width="9" height="9" rx="1.6"/><path d="M10 4.5v3M14 4.5v3M10 16.5v3M14 16.5v3M4.5 10h3M4.5 14h3M16.5 10h3M16.5 14h3"/>',
} as const

export type IconName = keyof typeof ICON_PATHS

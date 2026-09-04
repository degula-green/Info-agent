export function sanitizeHTML(value: string) { return value }
export function sanitizeMarkdownHTML(value: string) { return value }
export function safeMarkdownToHTML(value: string) { return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') }

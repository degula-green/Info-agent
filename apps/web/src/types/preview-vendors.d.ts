declare module 'docx-preview' {
  export function renderAsync(data: ArrayBuffer | Blob, bodyContainer: HTMLElement, styleContainer?: HTMLElement, options?: Record<string, unknown>): Promise<unknown>
}

declare module 'pptx-preview' {
  export function init(container: HTMLElement, options?: Record<string, unknown>): { preview(data: ArrayBuffer | Blob): Promise<unknown> }
}

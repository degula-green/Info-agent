declare module 'xlsx-preview' {
  export interface XlsxOptions {
    output?: 'string' | 'arrayBuffer'
    separateSheets?: boolean
    minimumRows?: number
    minimumCols?: number
  }

  export function xlsx2Html(
    data: ArrayBuffer | Blob | File,
    options?: XlsxOptions,
  ): Promise<string | ArrayBuffer | string[] | Promise<ArrayBuffer>[]>

  const xlsxPreview: { xlsx2Html: typeof xlsx2Html }
  export default xlsxPreview
}

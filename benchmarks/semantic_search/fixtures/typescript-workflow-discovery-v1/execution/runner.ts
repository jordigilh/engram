export function validateWorkflowParameters(
  required: ReadonlySet<string>,
  supplied: Record<string, string>,
): Record<string, string> {
  const missing = [...required].filter((key) => !(key in supplied));
  const unknown = Object.keys(supplied).filter((key) => !required.has(key));
  if (missing.length > 0 || unknown.length > 0) {
    throw new Error(`invalid workflow parameters: missing=${missing}, unknown=${unknown}`);
  }
  return supplied;
}

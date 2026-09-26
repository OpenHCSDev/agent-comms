/** JSON.parse alone silently accepts duplicate object keys, including deny→approve. */
export function parseUniqueJson(text) {
  const value = JSON.parse(text); // Validate grammar before the duplicate-key scan.
  const stack = [];
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char === '"') {
      const start = index;
      for (index++; index < text.length; index++) {
        if (text[index] === '\\') { index++; continue; }
        if (text[index] === '"') break;
      }
      const current = stack.at(-1);
      if (current?.keys && current.expectKey) {
        const key = JSON.parse(text.slice(start, index + 1));
        if (current.keys.has(key)) throw new Error('Duplicate JSON key');
        current.keys.add(key);
        current.expectKey = false;
      }
    } else if (char === '{') {
      stack.push({ keys: new Set(), expectKey: true });
    } else if (char === '[') {
      stack.push({ keys: undefined });
    } else if (char === '}' || char === ']') {
      stack.pop();
    } else if (char === ',') {
      const current = stack.at(-1);
      if (current?.keys) current.expectKey = true;
    }
  }
  return value;
}

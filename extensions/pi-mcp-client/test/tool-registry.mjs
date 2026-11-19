/** Test fixture for Pi's registered/active tool distinction. No calls leave this object. */
export class ToolRegistry {
  #tools = new Map();
  #active = new Set();

  constructor(initial = []) {
    for (const tool of initial) this.registerTool(tool);
  }

  registerTool(tool) {
    if (this.#tools.has(tool.name)) throw new Error('Duplicate fixture tool');
    this.#tools.set(tool.name, tool);
    this.#active.add(tool.name);
  }

  getAllTools() {
    return [...this.#tools.values()];
  }

  getActiveTools() {
    return [...this.#active];
  }

  setActiveTools(names) {
    this.#active = new Set(names.filter((name) => this.#tools.has(name)));
  }

  tool(name) {
    const tool = this.#tools.get(name);
    if (!tool) throw new Error('Unknown fixture tool');
    return tool;
  }
}

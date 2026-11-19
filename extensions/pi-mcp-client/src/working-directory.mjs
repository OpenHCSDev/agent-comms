import { realpath, stat } from 'node:fs/promises';
import { isAbsolute, relative, sep } from 'node:path';

/** The directory declaration owns its scope, trust requirement and launch root. */
export class WorkingDirectory {
  static #members = new Map();

  static get declaredName() {
    return this.name.replace(/Directory$/, '').toLowerCase();
  }

  static get defaultName() {
    return ProjectDirectory.declaredName;
  }

  static declare(member) {
    const name = member.declaredName;
    if (WorkingDirectory.#members.has(name)) throw new Error('Duplicate MCP working directory');
    WorkingDirectory.#members.set(name, member);
  }

  static names() {
    return [...WorkingDirectory.#members.keys()];
  }

  static of(name) {
    const member = WorkingDirectory.#members.get(name);
    if (!member) throw new Error('Unsupported MCP working directory');
    return new member();
  }

  validateScope(_scope) {}

  trusted(projectTrusted) {
    return projectTrusted;
  }

  path(entry) {
    return entry.projectRoot;
  }

  validateRoot(_root, _entry) {}

  async resolve(entry) {
    const path = this.path(entry);
    if (typeof path !== 'string' || !isAbsolute(path)) {
      throw new Error('Absolute MCP working directory required');
    }
    const root = await realpath(path);
    if (!(await stat(root)).isDirectory()) throw new Error('MCP working directory is not a directory');
    this.validateRoot(root, entry);
    return root;
  }
}

export class ProjectDirectory extends WorkingDirectory {
  static { WorkingDirectory.declare(this); }
}

/** User-owned configuration may opt into its own root without trusting project resources. */
export class AgentDirectory extends WorkingDirectory {
  static { WorkingDirectory.declare(this); }

  validateScope(scope) {
    if (scope !== 'user') throw new Error('Agent MCP working directory requires user scope');
  }

  trusted(_projectTrusted) {
    return true;
  }

  path(entry) {
    return entry.agentDir;
  }

  validateRoot(root, entry) {
    const path = relative(entry.projectRoot, root);
    if (path === '' || (!isAbsolute(path) && path !== '..' && !path.startsWith(`..${sep}`))) {
      throw new Error('User MCP working directory must be outside the project');
    }
    if (!isAbsolute(entry.declaration.transport.command)) {
      throw new Error('Agent MCP working directory requires an absolute executable');
    }
  }
}

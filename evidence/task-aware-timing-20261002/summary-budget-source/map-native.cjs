// Read-only AST adapter for the existing installed Jiti/Babel parser. No eval.
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const native = process.argv[2];
const output = process.argv[3];
const parse = require(path.join(native, 'node_modules/jiti/dist/babel.cjs'));
const roots = ['stack', path.join(native, 'dist/core/compaction'),
  path.join(native, 'node_modules/@earendil-works/pi-ai/dist/api')];
const terms = new Set(['CompactionPolicy', 'ContextBudget', 'ContextBudgetRequest',
  'summaryTokens', 'requireSummaryOutput', 'packSummary', 'requireContext',
  'maxTokens', 'max_output_tokens', 'reasoning', 'reasoning_tokens',
  'output_tokens', 'completeSummarization', 'generateSummaryWithUsage',
  'createSummarizationOptions', 'buildRequestBody', 'buildBaseOptions']);
const result = { revision: cp.execFileSync('git', ['rev-parse', 'HEAD'], {encoding:'utf8'}).trim(),
  roots, parser: 'existing installed Jiti Babel pre-transform AST', parsed: [],
  failures: [], declarations: [], references: [], imports: [],
  limitations: ['Syntactic references do not prove dynamic binding or composer replacement capabilities.',
    'Python owners use the existing authoritative Package AST receipts; this adapter covers native roots.',
    'Patch files are not JavaScript; their applied native definitions are covered by the installed artifact.'] };
for (const root of roots) {
  for (const name of fs.readdirSync(root)) {
    if (!/\.(mjs|js)$/.test(name) || name.startsWith('test-')) continue;
    const file = path.join(root, name);
    const source = fs.readFileSync(file, 'utf8');
    const visit = (node, owners=[]) => {
      if (!node || typeof node !== 'object') return;
      const name = node.id?.name ?? node.key?.name;
      const declaration = ['ClassDeclaration','FunctionDeclaration','ClassMethod','ClassPrivateMethod'].includes(node.type);
      const here = declaration && name ? [...owners, name] : owners;
      const record = {file, line:node.loc?.start.line, owner:here.join('.'), kind:node.type};
      if (declaration && name && terms.has(name)) result.declarations.push({...record, name});
      if (node.type === 'Identifier' && terms.has(node.name))
        result.references.push({...record, name:node.name, source:source.slice(node.start,node.end)});
      if (node.type === 'ImportDeclaration') result.imports.push({...record, from:node.source.value,
        names:node.specifiers.map(s=>s.imported?.name ?? s.local.name)});
      for (const [key, value] of Object.entries(node)) {
        if (['loc','start','end','leadingComments','trailingComments','innerComments','extra'].includes(key)) continue;
        if (Array.isArray(value)) value.forEach(child=>visit(child,here));
        else if (value?.type) visit(value,here);
      }
    };
    const transformed = parse({source,filename:file,async:true,babel:{plugins:[()=>({
      pre(file) {visit(file.ast);}
    })]}});
    if (transformed.error) result.failures.push({file,error:transformed.error.message});
    else result.parsed.push(file);
  }
}
fs.writeFileSync(output, JSON.stringify(result,null,2)+'\n');
console.log(JSON.stringify({parsed:result.parsed.length,failures:result.failures,
  declarations:result.declarations.length,references:result.references.length,output}));

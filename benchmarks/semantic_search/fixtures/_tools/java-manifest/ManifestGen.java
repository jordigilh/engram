import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.stream.Stream;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

/**
 * Generate manifest.json for the java-workflow-discovery fixture using the
 * javac Tree API. Every top-level type and every method, constructor and field
 * becomes a unit with a real source span derived from javac positions.
 *
 * <pre>
 * javac -d /tmp/java-manifest ManifestGen.java
 * java -cp /tmp/java-manifest ManifestGen &lt;fixture-root&gt; [output]
 * </pre>
 */
public final class ManifestGen {

    private static final String FIXTURE_ID = "java-workflow-discovery-v1";
    private static final String REPOSITORY = "synthetic/java-workflow-discovery";
    private static final String REVISION = "fixture-v1";

    private record Unit(String unitId, String path, String symbol, String kind,
                        int startLine, int endLine, String anchor) {
    }

    private static final class Generator extends TreePathScanner<Void, Void> {
        private final CompilationUnitTree unit;
        private final String relativePath;
        private final List<String> lines;
        private final String source;
        private final SourcePositions positions;
        private final List<Unit> units = new ArrayList<>();

        Generator(CompilationUnitTree unit, String relativePath, Trees trees) {
            this.unit = unit;
            this.relativePath = relativePath;
            try {
                this.source = unit.getSourceFile().getCharContent(true).toString();
            } catch (IOException error) {
                throw new IllegalStateException("cannot read " + relativePath, error);
            }
            this.lines = source.lines().toList();
            this.positions = trees.getSourcePositions();
        }

        @Override
        public Void visitClass(ClassTree tree, Void unused) {
            scanClass(tree, "");
            return null;
        }

        private void scanClass(ClassTree tree, String prefix) {
            String typeName = tree.getSimpleName().toString();
            String qualified = prefix.isEmpty() ? typeName : prefix + "." + typeName;
            addUnit(qualified, typeName, kindOf(tree),
                    positions.getStartPosition(unit, tree), positions.getEndPosition(unit, tree));
            for (Tree member : tree.getMembers()) {
                if (member instanceof MethodTree method) {
                    String memberName = method.getName().contentEquals("<init>")
                            ? "constructor" : method.getName().toString();
                    String kind = method.getReturnType() == null ? "constructor" : "method";
                    addUnit(qualified + "." + memberName, memberName, kind,
                            positions.getStartPosition(unit, method),
                            positions.getEndPosition(unit, method));
                } else if (member instanceof VariableTree variable) {
                    // Record components are accessors, not fields; instance
                    // fields are implementation detail. Only static fields of
                    // interest become retrieval units.
                    boolean isStatic = variable.getModifiers().getFlags()
                            .contains(javax.lang.model.element.Modifier.STATIC);
                    if (isStatic) {
                        addUnit(qualified + "." + variable.getName(),
                                variable.getName().toString(), "field",
                                positions.getStartPosition(unit, variable),
                                positions.getEndPosition(unit, variable));
                    }
                } else if (member instanceof ClassTree nested) {
                    scanClass(nested, qualified);
                }
            }
        }

        private static String kindOf(ClassTree tree) {
            return switch (tree.getKind()) {
                case RECORD -> "record";
                case INTERFACE -> "interface";
                case CLASS -> "class";
                default -> throw new IllegalStateException("unsupported type kind: " + tree.getKind());
            };
        }

        private void addUnit(String memberPath, String symbol, String kind,
                             long start, long end) {
            if (start < 0 || end <= start) {
                throw new IllegalStateException("missing source span for " + memberPath);
            }
            int startLine = (int) unit.getLineMap().getLineNumber(start);
            int endLine = (int) unit.getLineMap().getLineNumber(end - 1);
            while (startLine < endLine && lines.get(startLine - 1).strip().startsWith("@")) {
                startLine++;
            }
            if (startLine < 1 || endLine > lines.size()) {
                throw new IllegalStateException("span out of range for " + memberPath);
            }
            String anchor = lines.get(startLine - 1).strip();
            long declarationStart = unit.getLineMap().getStartPosition(startLine);
            if (!source.substring((int) declarationStart).stripLeading().startsWith(anchor)) {
                throw new IllegalStateException(
                        "span does not begin with its declaration anchor: " + memberPath
                        + " startLine=" + startLine + " anchor=" + anchor
                        + " actual=" + source.substring((int) declarationStart).stripLeading()
                                .lines().findFirst().orElse(""));
            }
            if (anchor.isEmpty()) {
                throw new IllegalStateException("empty declaration anchor: " + memberPath);
            }
            units.add(new Unit(
                    relativePath + "::" + memberPath,
                    relativePath,
                    symbol,
                    kind,
                    startLine,
                    endLine,
                    anchor));
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1 || args.length > 2) {
            System.err.println("usage: ManifestGen <fixture-root> [output.json]");
            System.exit(2);
        }
        Path root = Path.of(args[0]).toAbsolutePath().normalize();
        Path output = args.length == 2
                ? Path.of(args[1]) : root.resolve("manifest.json");

        List<Path> files;
        try (Stream<Path> walk = Files.walk(root)) {
            files = walk.filter(Files::isRegularFile)
                    .filter(path -> path.toString().endsWith(".java"))
                    .filter(path -> !relative(root, path).startsWith("_tools/"))
                    .sorted(Comparator.comparing(path -> relative(root, path)))
                    .toList();
        }
        if (files.isEmpty()) {
            throw new IllegalStateException("no Java sources found under " + root);
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("a JDK with javax.tools is required");
        }
        List<Unit> units = new ArrayList<>();
        long totalBytes = 0;
        List<String> digestLines = new ArrayList<>();
        try (StandardJavaFileManager fileManager =
                     compiler.getStandardFileManager(null, Locale.ROOT, StandardCharsets.UTF_8)) {
            List<JavaFileObject> objects = new ArrayList<>();
            for (Path file : files) {
                for (JavaFileObject object : fileManager.getJavaFileObjectsFromPaths(List.of(file))) {
                    objects.add(object);
                }
                totalBytes += Files.size(file);
                digestLines.add(relative(root, file) + "\t" + sha256(Files.readAllBytes(file)));
            }
            JavacTask task = (JavacTask) compiler.getTask(
                    null, fileManager, diagnostic -> {
                        throw new IllegalStateException(diagnostic.toString());
                    }, List.of("-proc:none"), null, objects);
            Iterable<? extends CompilationUnitTree> parsed = task.parse();
            Trees trees = Trees.instance(task);
            for (CompilationUnitTree unit : parsed) {
                String relativePath = relative(root, Path.of(unit.getSourceFile().toUri()));
                Generator generator = new Generator(unit, relativePath, trees);
                generator.scan(unit, null);
                units.addAll(generator.units);
            }
        }

        String snapshot = sha256(String.join("\n", digestLines).getBytes(StandardCharsets.UTF_8));
        String json = render(files.size(), totalBytes, snapshot, units);
        Files.writeString(output, json + "\n", StandardCharsets.UTF_8);
        System.out.printf("wrote %s: %d files, %d bytes, %d units, snapshot %s%n",
                output, files.size(), totalBytes, units.size(), snapshot);
    }

    private static String relative(Path root, Path file) {
        return root.relativize(file.toAbsolutePath().normalize()).toString()
                .replace('\\', '/');
    }

    private static String sha256(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }

    private static String render(int fileCount, long totalBytes, String snapshot,
                                 List<Unit> units) {
        StringBuilder json = new StringBuilder();
        json.append("{\n");
        json.append("  \"schema_version\": 1,\n");
        json.append("  \"fixture_id\": \"").append(FIXTURE_ID).append("\",\n");
        json.append("  \"language\": \"java\",\n");
        json.append("  \"source\": {\n");
        json.append("    \"repository\": \"").append(REPOSITORY).append("\",\n");
        json.append("    \"revision\": \"").append(REVISION).append("\",\n");
        json.append("    \"root\": \".\",\n");
        json.append("    \"include\": [\"**/*.java\"],\n");
        json.append("    \"exclude\": [\"_tools/**\"],\n");
        json.append("    \"files\": ").append(fileCount).append(",\n");
        json.append("    \"bytes\": ").append(totalBytes).append(",\n");
        json.append("    \"snapshot_sha256\": \"").append(snapshot).append("\"\n");
        json.append("  },\n");
        json.append("  \"units\": [\n");
        for (int index = 0; index < units.size(); index++) {
            Unit unit = units.get(index);
            Map<String, String> fields = new LinkedHashMap<>();
            fields.put("unit_id", unit.unitId());
            fields.put("path", unit.path());
            fields.put("symbol", unit.symbol());
            fields.put("kind", unit.kind());
            fields.put("start_line", Integer.toString(unit.startLine()));
            fields.put("end_line", Integer.toString(unit.endLine()));
            fields.put("declaration_anchor", unit.anchor());
            json.append("    {");
            int fieldIndex = 0;
            for (Map.Entry<String, String> entry : fields.entrySet()) {
                if (fieldIndex++ > 0) {
                    json.append(", ");
                }
                json.append('"').append(entry.getKey()).append("\": ");
                if (entry.getKey().endsWith("_line")) {
                    json.append(entry.getValue());
                } else {
                    json.append('"').append(escape(entry.getValue())).append('"');
                }
            }
            json.append('}');
            json.append(index + 1 == units.size() ? "\n" : ",\n");
        }
        json.append("  ]\n");
        json.append("}\n");
        return json.toString();
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}

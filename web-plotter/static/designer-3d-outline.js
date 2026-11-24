import { BufferGeometry, Color, DoubleSide, Float32BufferAttribute, Mesh, MeshStandardMaterial } from "./three-3d.js";

const verticesOf = ([a, b, c]) => [[a, b], [b, c], [c, a]];

export function clothBoundary({ vertices, triangles }) {
  const edges = new Map();
  for (const triangle of triangles) for (const [a, b] of verticesOf(triangle)) {
    const key = Math.min(a, b) + ":" + Math.max(a, b);
    const existing = edges.get(key);
    if (existing) existing.count++;
    else edges.set(key, { a, b, count: 1 });
  }
  return [...edges.values()].filter(edge => edge.count === 1).map(({ a, b }) => [vertices[a], vertices[b]]);
}

// Expand an edge to a screen-space stroke. It stays readable as the model is
// zoomed and still goes through the same depth test as the rest of the scene.
export function createClothOutline(segments, viewport, near) {
  const positions = [];
  const opposite = [];
  const sides = [];
  const across = [];
  const indices = [];
  for (const [a, b] of segments) {
    const index = positions.length / 3;
    positions.push(...a, ...a, ...b, ...b);
    opposite.push(...b, ...b, ...a, ...a);
    sides.push(1, -1, -1, 1);
    across.push(1, -1, 1, -1);
    indices.push(index, index + 1, index + 2, index + 1, index + 3, index + 2);
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute(positions, 3));
  geometry.setAttribute("opposite", new Float32BufferAttribute(opposite, 3));
  geometry.setAttribute("strokeSide", new Float32BufferAttribute(sides, 1));
  geometry.setAttribute("strokeAcross", new Float32BufferAttribute(across, 1));
  geometry.setIndex(indices);
  const ink = { value: new Color("#152034") };
  const radius = { value: 3 };
  const material = new MeshStandardMaterial({ side: DoubleSide, depthWrite: false, toneMapped: false });
  material.customProgramCacheKey = () => "plotter-cloth-outline";
  material.onBeforeCompile = shader => {
    Object.assign(shader.uniforms, { outlineViewport: viewport, outlineNear: near, outlineInk: ink, outlineRadius: radius });
    shader.vertexShader = shader.vertexShader.replace("#include <common>", `
      #include <common>
      attribute vec3 opposite;
      attribute float strokeSide;
      attribute float strokeAcross;
      varying float sideOnStroke;
      uniform vec2 outlineViewport;
      uniform float outlineNear;
      uniform float outlineRadius;
    `).replace("#include <project_vertex>", `
      vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
      vec4 other = modelViewMatrix * vec4(opposite, 1.0);
      sideOnStroke = strokeAcross;
      float limit = -outlineNear * 1.01;
      if (mvPosition.z > limit && other.z > limit) {
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
      } else {
        if (mvPosition.z > limit) mvPosition = mix(mvPosition, other, (limit - mvPosition.z) / (other.z - mvPosition.z));
        if (other.z > limit) other = mix(other, mvPosition, (limit - other.z) / (mvPosition.z - other.z));
        vec4 a = projectionMatrix * mvPosition;
        vec4 b = projectionMatrix * other;
        vec2 delta = (b.xy / b.w - a.xy / a.w) * outlineViewport;
        vec2 tangent = delta / max(length(delta), 0.001);
        vec2 normalToEdge = vec2(-tangent.y, tangent.x);
        vec2 offset = (normalToEdge * strokeSide * outlineRadius * 2.0 - tangent) / outlineViewport;
        a.xy += offset * a.w;
        a.z -= 0.000035 * a.w;
        gl_Position = a;
      }
    `);
    shader.fragmentShader = shader.fragmentShader.replace("#include <common>", `
      #include <common>
      varying float sideOnStroke;
      uniform vec3 outlineInk;
    `).replace("#include <opaque_fragment>", `
      gl_FragColor = vec4(mix(outlineInk, vec3(1.0), smoothstep(0.45, 0.63, abs(sideOnStroke))), 1.0);
    `);
  };
  const mesh = new Mesh(geometry, material);
  mesh.frustumCulled = false;
  mesh.renderOrder = 6;
  return {
    mesh,
    paint(color, pixels) {
      ink.value.set(color);
      radius.value = pixels;
    },
  };
}

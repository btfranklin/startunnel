precision highp float;

varying vec2 v_uv;
uniform vec2 u_resolution;
uniform float u_time;

vec2 hash22(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
    return fract(sin(p) * 43758.5453);
}

float noise2(vec2 p) {
    vec2 cell = floor(p);
    vec2 f = fract(p);
    vec2 s = f * f * (3.0 - 2.0 * f);
    float a = dot(hash22(cell), vec2(0.5));
    float b = dot(hash22(cell + vec2(1.0, 0.0)), vec2(0.5));
    float c = dot(hash22(cell + vec2(0.0, 1.0)), vec2(0.5));
    float d = dot(hash22(cell + vec2(1.0, 1.0)), vec2(0.5));
    return mix(mix(a, b, s.x), mix(c, d, s.x), s.y);
}

// The gap between the two nearest cells forms the base water-light pattern.
float cellSeam(vec2 p) {
    vec2 cell = floor(p);
    vec2 f = fract(p);
    float nearest = 8.0;
    float second = 8.0;
    for (int y = -1; y <= 1; y++) {
        for (int x = -1; x <= 1; x++) {
            vec2 offset = vec2(float(x), float(y));
            vec2 point = offset + 0.22 + 0.56 * hash22(cell + offset);
            vec2 delta = point - f;
            float distanceToPoint = dot(delta, delta);
            if (distanceToPoint < nearest) {
                second = nearest;
                nearest = distanceToPoint;
            } else if (distanceToPoint < second) {
                second = distanceToPoint;
            }
        }
    }
    return second - nearest;
}

void main() {
    vec2 p = (gl_FragCoord.xy - 0.5 * u_resolution) / min(u_resolution.x, u_resolution.y);
    float radius = length(p);
    float angle = atan(p.y, p.x);
    float t = u_time * 0.10;

    // Distort the surface like water that turns into a deeper central channel.
    float twist = (0.5 - radius) * 1.5 + 0.06 * sin(radius * 17.0 - t);
    vec2 flow = mat2(cos(twist), -sin(twist), sin(twist), cos(twist)) * p;
    flow += 0.018 * vec2(sin(angle * 5.0 + radius * 21.0 - t), cos(angle * 4.0 - radius * 18.0 + t));
    float warpA = noise2(flow * 6.5 + vec2(t, -t * 0.7));
    float warpB = noise2(flow * 8.0 + vec2(-t * 0.8, t));
    vec2 warped = flow + 0.075 * vec2(warpA - 0.5, warpB - 0.5);

    float broad = cellSeam(warped * 10.0 + vec2(t * 0.38, -t * 0.25));
    float fine = cellSeam(warped * 18.0 + vec2(-t * 0.4, t * 0.3));
    float shimmer = noise2(warped * 12.0 + vec2(t * 0.7, -t * 0.5));
    float pools = noise2(warped * 5.0 + vec2(-t * 0.3, t * 0.4));
    float edgeEnergy = smoothstep(0.05, 0.46, radius);
    float centerDepth = 1.0 - smoothstep(0.04, 0.37, radius);
    float softSeam = 1.0 - smoothstep(0.035, 0.31, broad);
    float caustic = 1.0 - smoothstep(0.008, 0.105, broad);
    float fineGlow = 1.0 - smoothstep(0.01, 0.16, fine);
    float lightPockets = smoothstep(0.32, 0.68, pools);
    float ripples = sin(radius * 48.0 + angle * 2.0 + (warpA - warpB) * 7.0 - t * 2.3);
    float rippleLight = smoothstep(0.30, 0.98, ripples) * (0.25 + 0.75 * shimmer);

    vec3 color = mix(vec3(0.0, 0.035, 0.13), vec3(0.0, 0.12, 0.35), edgeEnergy);
    color += vec3(0.0, 0.055, 0.18) * (0.35 + pools) * (1.0 - centerDepth * 0.45);
    color += vec3(0.0, 0.20, 0.49) * lightPockets * (0.2 + edgeEnergy * 0.42);
    color += vec3(0.005, 0.25, 0.63) * softSeam * (0.15 + 0.46 * shimmer) * (0.45 + 0.55 * edgeEnergy);
    color += vec3(0.04, 0.44, 0.93) * caustic * (0.18 + 0.74 * shimmer) * (0.32 + 0.68 * edgeEnergy);
    color += vec3(0.07, 0.33, 0.68) * fineGlow * (0.15 + 0.55 * lightPockets);
    color += vec3(0.0, 0.12, 0.32) * rippleLight * (0.25 + 0.7 * edgeEnergy);

    float hotSpot = pow(caustic * shimmer * (0.45 + 0.55 * edgeEnergy), 3.0);
    color += vec3(0.45, 0.75, 1.0) * hotSpot * 0.75;
    color *= 1.0 - centerDepth * 0.38;

    float rim = exp(-pow((radius - 0.484) * 95.0, 2.0));
    float innerLight = exp(-pow((radius - 0.444) * 26.0, 2.0));
    color += vec3(0.25, 0.72, 1.0) * rim * (0.60 + 0.38 * shimmer);
    color += vec3(0.03, 0.28, 0.66) * innerLight * 0.34;

    float alpha = 1.0 - smoothstep(0.492, 0.501, radius);
    gl_FragColor = vec4(min(color, vec3(1.0)), alpha);
}

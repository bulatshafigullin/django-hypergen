from setuptools import Extension, setup
from Cython.Build import cythonize

extensions = [
    Extension("hypergen.hg_core", ["src/hypergen/hg_core.pyx"]),
    Extension("hypergen.ultragen", ["src/hypergen/ultragen.pyx"]),
    Extension("examples.gameofcython.gameofcython", ["examples/gameofcython/gameofcython.pyx"]),]

setup(
    name="ultra",
    ext_modules=cythonize(extensions, annotate=True),
    zip_safe=False,
)

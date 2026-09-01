const stack: string[] = [];

export const registerDialog = (id: string) => {
  stack.push(id);
  return () => {
    const index = stack.lastIndexOf(id);
    if (index >= 0) stack.splice(index, 1);
  };
};

export const isTopDialog = (id: string) => stack[stack.length - 1] === id;
